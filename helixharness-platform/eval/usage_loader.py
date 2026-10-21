"""HelixHarness usage telemetry loader and dashboard renderer.

Reads HelixHarness ``docs/eval/{feature}/eval-log.json`` files from one or more
project roots and renders a compact HTML dashboard for HelixHarness Observatory.

HelixHarness only records facts; this module aggregates the latest telemetry
schema into user x project views without assigning a stable proficiency label
when samples are small.

模块结构（分区导航）：
  §1 常量与配色           — STAGE_ORDER / STAGE_LABELS / _COLORS / METRIC_TIPS
  §2 数据加载             — _load_ai_insights / _load_eval_log / discover_projects
  §3 数据解析（单条）      — _esc / _quality / _artifacts / _resource_usage ...
  §4 数据聚合（多条→统计） — aggregate_feature / aggregate_all / _summarize_user_rows
  §5 格式化               — _fmt_tokens / _fmt_duration / _fmt_code_delta ...
  §6 HTML 渲染（概览）     — render_usage_global_overview_html / _card / _mini_metric
  §7 HTML 渲染（明细表）   — render_usage_overview_html / _feature_table_html ...
  §8 HTML 渲染（详情弹窗） — _feature_detail_page / _user_detail_page ...
  §9 AI 解读渲染           — _format_ai_answer / _highlight_ai_text ...
  §10 CSS 样式定义         — <style> 块（helixharness-* 类）
"""
from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eval.code_metrics import ARCHIVE_REASONS, archive_code_delta
from eval.feature_exclusions import FeatureExclusionStore
from eval.ledger_paths import project_ledger_files
from eval.stage_supplements import (
    LEDGER_SCHEMA,
    LEDGER_SCHEMA_VERSION,
    LinkedStageSupplement,
)
from eval.telemetry_materialization import (
    index_linked_supplements,
    materialize_eval_log,
)

STAGE_ORDER = ["requirement", "coding", "archive"]
STAGE_LABELS = {
    "requirement": "需求与计划",
    "coding": "开发与验证",
    "archive": "归档",
}
PLATFORM_LABELS = {
    "claude": "Claude Code",
    "claude-code": "Claude Code",
    "codex": "Codex",
}
REVISION_REASON_LABELS = {
    "clarification": "澄清修正",
    "finding_fix": "问题修复",
    "gate_fix": "门禁补证",
    "scope_change": "范围变化",
    "format_only": "格式修正",
    "unknown": "修正",
}
SUPPLEMENT_REASON_LABELS = {
    "clarification": "需求澄清",
    "context_supplement": "上下文补充",
    "format_only": "格式补充",
    "unknown": "阶段补充",
}
COMMANDS = ["propose", "apply", "archive"]
PROJECT_COMMANDS = COMMANDS
BREAKDOWN_KEYS = ["brainstorming", "clarification", "course_correction", "context_supplement"]
BREAKDOWN_LABELS = {
    "brainstorming": "方案讨论",
    "clarification": "澄清",
    "course_correction": "纠偏",
    "context_supplement": "上下文补充",
}
RESOURCE_KEYS = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"]
CODE_DELTA_KEYS = [
    "files_changed",
    "lines_added",
    "lines_deleted",
    "prod_files_changed",
    "prod_lines_added",
    "prod_lines_deleted",
    "test_files_changed",
    "test_lines_added",
    "test_lines_deleted",
    "docs_files_changed",
    "docs_lines_added",
    "docs_lines_deleted",
    "other_files_changed",
    "other_lines_added",
    "other_lines_deleted",
    "binary_files_changed",
]
RISK_ORDER = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
_PANEL = "#FFFFFF"
_BORDER = "#E5E6EB"
_TEXT = "#1D2129"
_MUTED = "#4E5969"
_ACCENT = "#1677FF"
_GREEN = "#52C41A"
_GOLD = "#FAAD14"
_RED = "#FF4D4F"
_GRAY = "#86909C"
_BLUE = "#4096FF"
_PURPLE = "#722ED1"
_CYAN = "#13A8A8"
AI_INSIGHTS_FILE = "ai_insights.json"


def _stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage)


def _load_ai_insights(roots: list[str], current_data_hash: str = "") -> dict:
    candidates = []
    env_path = os.environ.get("HELIXHARNESS_PLATFORM_USAGE_AI_INSIGHTS", "").strip()
    if env_path:
        candidates.append(env_path)
    cache_dir = Path(os.environ.get("HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE", str(Path(__file__).resolve().parent / ".cache")))
    candidates.append(str(cache_dir / AI_INSIGHTS_FILE))
    if roots:
        parents = [os.path.dirname(os.path.abspath(r)) for r in roots if r]
        if parents:
            common_parent = os.path.commonpath(parents)
            candidates.append(os.path.join(common_parent, AI_INSIGHTS_FILE))
            candidates.append(os.path.join(parents[0], AI_INSIGHTS_FILE))
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f) or {}
            if isinstance(data, dict):
                data["_path"] = path
                cached_hash = str(data.get("data_state_hash") or "")
                if (
                    current_data_hash
                    and cached_hash != current_data_hash
                    and data.get("status") in {"success", "partial"}
                ):
                    data["status"] = "stale"
                    data["team"] = None
                    data["features"] = {}
                    data["installations"] = {}
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _load_eval_log(
    path: str,
    linked_records: dict[str, LinkedStageSupplement] | None = None,
) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        return {"_invalid": True, "_log_path": path, "_validation_errors": [f"read_error:{exc}"]}
    except json.JSONDecodeError as exc:
        return {"_invalid": True, "_log_path": path, "_validation_errors": [f"invalid_json:{exc.msg}"]}

    errors = []
    if not isinstance(data, dict):
        errors.append("root_must_be_object")
    elif data.get("schema") == LEDGER_SCHEMA:
        if data.get("schema_version") != LEDGER_SCHEMA_VERSION:
            errors.append(f"schema_version_must_be_{LEDGER_SCHEMA_VERSION}")
        for key in ("feature", "project", "requirement", "created_at", "last_updated", "status", "entries"):
            if key not in data:
                errors.append(f"missing:{key}")
        if not isinstance(data.get("entries"), list):
            errors.append("entries_must_be_array")
        else:
            required = (
                "entry_id",
                "helixharness_version",
                "command",
                "stage_kind",
                "attempt",
                "trigger",
                "platform",
                "session_hash",
                "started_at",
                "ended_at",
                "status",
                "actor",
                "checkpoint",
                "artifacts",
                "quality",
            )
            for index, entry in enumerate(data["entries"]):
                if not isinstance(entry, dict):
                    errors.append(f"entry[{index}]_must_be_object")
                    continue
                for key in required:
                    if key not in entry:
                        errors.append(f"entry[{index}].missing:{key}")
    else:
        errors.append("unsupported_ledger_schema")
    if errors:
        return {"_invalid": True, "_log_path": path, "_validation_errors": errors}
    data = materialize_eval_log(data, linked_records)
    data["_log_path"] = path
    return data


def discover_projects(
    roots: list[str],
    rejected_paths: set[str] | None = None,
    linked_supplements: list[LinkedStageSupplement] | None = None,
    excluded_features: set[tuple[str, str]] | None = None,
) -> list[dict]:
    projects = []
    seen = set()
    exclusions = FeatureExclusionStore().active_pairs() if excluded_features is None else excluded_features
    linked_by_path = index_linked_supplements(linked_supplements or [])
    for root in roots:
        root = os.path.expanduser((root or "").strip())
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        repository = os.path.basename(os.path.normpath(root))
        if root in seen:
            continue
        seen.add(root)
        features = []
        invalid_logs = []
        for ledger_path in project_ledger_files(root):
            log_path = str(ledger_path)
            feature_name = os.path.basename(os.path.dirname(log_path))
            if (repository, feature_name) in exclusions:
                continue
            if rejected_paths and os.path.abspath(log_path) in rejected_paths:
                invalid_logs.append({
                    "_invalid": True,
                    "_log_path": log_path,
                    "_validation_errors": ["consumer_quality_gate:INVALID"],
                })
                continue
            log = _load_eval_log(log_path, linked_by_path.get(os.path.realpath(log_path)))
            if log and log.get("_invalid"):
                invalid_logs.append(log)
            elif log:
                log["feature"] = _feature_name(log.get("feature"), os.path.basename(os.path.dirname(log_path)) or "?")
                features.append(log)
        if features or invalid_logs:
            first_project = _project(features[0], root) if features else {
                "project_name": os.path.basename(os.path.normpath(root)) or "未命名项目",
                "project_id": os.path.basename(os.path.normpath(root)) or "未命名项目",
            }
            projects.append({
                "project_name": first_project["project_name"],
                "project_id": first_project["project_id"],
                "project_dir": root,
                "features": features,
                "invalid_logs": invalid_logs,
            })
    return projects


def discover_dashboard_projects(
    roots: list[str], rejected_paths: set[str] | None = None,
    linked_supplements: list[LinkedStageSupplement] | None = None,
) -> list[dict]:
    """Admit the same materialized facts for dashboard rendering and AI generation.

    Apply the consumer Gate directly so CLI generation does not depend on whether
    the Web projection has already been refreshed. PARTIAL facts remain visible.
    """
    from eval.telemetry_quality import QualityStatus, assess_eval_log

    projects = discover_projects(roots, rejected_paths, linked_supplements)
    for project in projects:
        admitted = []
        for log in project["features"]:
            if assess_eval_log(log).status is QualityStatus.INVALID:
                project["invalid_logs"].append({
                    "_invalid": True, "_log_path": log["_log_path"],
                    "_validation_errors": ["consumer_quality_gate:INVALID"],
                })
            else:
                admitted.append(log)
        project["features"] = admitted
    return projects


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _short(value: str, n: int = 12) -> str:
    value = str(value or "unknown")
    return value if len(value) <= n else value[:n]


def _clean_name(value: Any) -> str:
    text = str(value or "").strip()
    return text if text and text.lower() not in {"unknown", "none", "null"} else ""


def _feature_name(value: Any, fallback: str = "?") -> str:
    return _clean_name(value) or fallback if isinstance(value, str) else fallback


def _project(eval_log: dict, fallback_root: str = "") -> dict:
    project = eval_log.get("project")
    project = project if isinstance(project, dict) else {}
    name = (
        _clean_name(project.get("name"))
        or (os.path.basename(os.path.normpath(fallback_root)) if fallback_root else "")
        or "未命名项目"
    )
    return {
        "project_id": name,
        "project_name": name,
        "project_dir": fallback_root,
        "helixharness_version": "unknown",
    }


def _actor(entry: dict) -> dict:
    actor = entry.get("actor")
    actor = actor if isinstance(actor, dict) else {}
    actor_key = _clean_name(actor.get("actor_key")) or "unknown"
    name = _clean_name(actor.get("user_name")) or "未命名用户"
    return {
        "user_id": name,
        "actor_key": actor_key,
        "user_name": name,
        "source": _clean_name(actor.get("user_name_source")),
        "confidence": actor.get("user_confidence") or "unknown",
        "user_name_source": _clean_name(actor.get("user_name_source")),
        "user_confidence": actor.get("user_confidence") or "unknown",
    }


def _is_countable_user(user_id: Any) -> bool:
    user_name = _clean_name(user_id)
    return bool(user_name and user_name not in {"unknown", "未命名用户"})


def _friction(entry: dict) -> dict:
    fr = entry.get("friction") if isinstance(entry.get("friction"), dict) else {}
    breakdown = fr.get("intervention_breakdown") if isinstance(fr.get("intervention_breakdown"), dict) else {}
    breakdown = {k: int((breakdown or {}).get(k, 0) or 0) for k in BREAKDOWN_KEYS}
    source = str(fr.get("classification_source") or "unavailable")
    events = [event for event in fr.get("intervention_events", []) if isinstance(event, dict)]
    deterministic_events = {
        str(event.get("event_id"))
        for event in events
        if event.get("event_id") and event.get("source") in {"command_fact", "artifact_rule"}
    }
    semantic_events = {
        str(event.get("event_id"))
        for event in events
        if event.get("event_id") and event.get("source") == "llm_session_classification"
    }
    interventions = fr.get("interventions")
    if interventions is None:
        interventions = sum(breakdown.values())
    deterministic_interventions = len(deterministic_events)
    semantic_interventions = len(semantic_events)
    if not events:
        if source in {"command_fact", "artifact_rule"}:
            deterministic_interventions = int(interventions or 0)
        elif source == "llm_session_classification":
            semantic_interventions = int(interventions or 0)
    return {
        "interventions": int(interventions or 0),
        "deterministic_interventions": deterministic_interventions,
        "semantic_interventions": semantic_interventions,
        "breakdown": breakdown,
        "events": events,
        "rework": fr.get("rework_from_previous"),
        "classification_source": source,
        "confidence": fr.get("confidence") or "unknown",
    }


def _quality(entry: dict) -> dict:
    q = entry.get("quality") if isinstance(entry.get("quality"), dict) else {}
    return {
        "gate_result": q.get("gate_result", "unknown"),
        "review_findings_count": q.get("review_findings_count"),
        "verify_passed": q.get("verify_passed"),
        "evidence_complete": q.get("evidence_complete"),
        "blocker_count": q.get("blocker_count"),
        "major_count": q.get("major_count"),
    }


def _artifacts(entry: dict) -> dict:
    a = entry.get("artifacts") if isinstance(entry.get("artifacts"), dict) else {}
    return {
        "produced_files": a.get("produced_files", entry.get("produced_files", [])) or [],
        "checkpoint_ref": a.get("checkpoint_ref", entry.get("checkpoint_ref")),
        "checkpoint_commit": a.get("checkpoint_commit", entry.get("checkpoint_commit")),
    }


def _execution_stats(entry: dict) -> dict:
    stats = entry.get("execution_stats") if isinstance(entry.get("execution_stats"), dict) else {}
    reads = stats.get("context_reads") if isinstance(stats.get("context_reads"), dict) else {}
    return {
        "tool_call_count": int(stats.get("tool_call_count", 0) or 0),
        "tool_error_count": int(stats.get("tool_error_count", 0) or 0),
        "subagent_spawn_count": int(stats.get("subagent_spawn_count", 0) or 0),
        "child_agent_scope": stats.get("child_agent_scope") or "unavailable",
        "context_reads": {
            key: int(reads.get(key, 0) or 0)
            for key in ("harness", "knowledge", "specs", "hooks")
        },
        "persistence_latency_ms": stats.get("persistence_latency_ms"),
        "record_write_count": int(stats.get("record_write_count", 0) or 0),
        "collection_status": stats.get("collection_status") or "unavailable",
    }


def _collection(entry: dict) -> dict:
    c = entry.get("collection") if isinstance(entry.get("collection"), dict) else {}
    return {
        "scope": c.get("scope", "none"),
        "source_log_count": int(c.get("source_log_count", 0) or 0),
        "events_scanned": int(c.get("events_scanned", 0) or 0),
        "messages_scanned": int(c.get("messages_scanned", 0) or 0),
        "messages_used": int(c.get("messages_used", 0) or 0),
        "user_turn_count": _to_int_or_none(c.get("user_turn_count")),
        "confidence": c.get("confidence", "unknown"),
        "collector_version": c.get("collector_version"),
        "parser_version": c.get("parser_version"),
        "host_versions": c.get("host_versions") if isinstance(c.get("host_versions"), list) else [],
        "semantic_status": c.get("semantic_status") or "unavailable",
        "classifier_version": c.get("classifier_version"),
        "semantic_coverage": c.get("semantic_coverage"),
        "candidate_user_turn_count": int(c.get("candidate_user_turn_count", 0) or 0),
        "classified_user_turn_count": int(c.get("classified_user_turn_count", 0) or 0),
        "malformed_event_count": int(c.get("malformed_event_count", 0) or 0),
    }


def _workflow(entry: dict) -> dict:
    wf = entry.get("workflow") if isinstance(entry.get("workflow"), dict) else {}
    revised_stage = _clean_name(wf.get("revised_stage_kind"))
    reason = _clean_name(wf.get("revision_reason"))
    revises = bool(wf.get("revises_previous_stage")) or bool(revised_stage)
    supplemented_stage = _clean_name(wf.get("supplemented_stage_kind"))
    supplement_reason = _clean_name(wf.get("supplement_reason"))
    supplements = bool(wf.get("supplements_previous_stage")) or bool(supplemented_stage)
    return {
        "previous_entry_id": wf.get("previous_entry_id"),
        "stage_repeated": bool(wf.get("stage_repeated", False)),
        "order_valid": bool(wf.get("order_valid", True)),
        "revises_previous_stage": revises,
        "revised_stage_kind": revised_stage,
        "revision_reason": reason,
        "supplements_previous_stage": supplements,
        "supplemented_stage_kind": supplemented_stage,
        "supplement_reason": supplement_reason,
    }


def _stage_profile_key(entry: dict) -> str:
    profile = _clean_name(entry.get("stage_profile"))
    if profile:
        return profile
    return "\0".join(
        str(entry.get(field) or "unknown")
        for field in ("command", "phase", "stage_kind")
    )


def _execution_attempt_metadata(entries: list[dict]) -> dict[str, dict]:
    """Describe logical attempts without counting continuation segments twice."""
    profiles: dict[str, dict[str, Any]] = {}
    prepared: list[tuple[dict, str, str]] = []
    for index, entry in enumerate(entries):
        workflow = _workflow(entry)
        profile_key = _stage_profile_key(entry)
        explicit_attempt = entry.get("attempt")
        if isinstance(explicit_attempt, int) and not isinstance(explicit_attempt, bool):
            run_key = f"attempt-{explicit_attempt}"
        else:
            run_key = str(entry.get("entry_id") or f"entry-{index}")
        profile = profiles.setdefault(profile_key, {"runs": [], "segments": {}, "entries": []})
        if run_key not in profile["runs"]:
            profile["runs"].append(run_key)
        profile["segments"].setdefault(run_key, []).append(str(entry.get("entry_id") or f"entry-{index}"))
        profile["entries"].append(str(entry.get("entry_id") or f"entry-{index}"))
        prepared.append((entry, profile_key, run_key))

    result: dict[str, dict] = {}
    for index, (entry, profile_key, run_key) in enumerate(prepared):
        profile = profiles[profile_key]
        runs = profile["runs"]
        segments = profile["segments"][run_key]
        entry_key = str(entry.get("entry_id") or f"entry-{index}")
        attempt_index = runs.index(run_key) + 1
        segment_index = segments.index(entry_key) + 1
        workflow = _workflow(entry)
        if attempt_index > 1 or workflow.get("stage_repeated"):
            execution_kind = "rerun"
        else:
            execution_kind = "initial"
        result[entry_key] = {
            "attempt_index": attempt_index,
            "attempt_count": len(runs),
            "segment_index": segment_index,
            "segment_count": len(segments),
            "execution_kind": execution_kind,
            "is_latest_entry": entry_key == profile["entries"][-1],
        }
    return result


def _to_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resource_usage(entry: dict) -> dict:
    ru = entry.get("resource_usage") if isinstance(entry.get("resource_usage"), dict) else {}
    result = {key: _to_int_or_none(ru.get(key)) for key in RESOURCE_KEYS}
    collection_status = str(ru.get("collection_status") or "").strip()
    unavailable_reason = ru.get("unavailable_reason")
    if not collection_status:
        collection_status = "unavailable"
        unavailable_reason = unavailable_reason or "missing_collection_status"
    elif collection_status not in {"pending", "collected", "unavailable"}:
        unavailable_reason = unavailable_reason or f"unsupported_collection_status:{collection_status}"
        collection_status = "unavailable"
    if collection_status == "collected" and result["total_tokens"] is None:
        collection_status = "unavailable"
        unavailable_reason = unavailable_reason or "missing_total_tokens"
    if collection_status != "collected":
        result = {key: None for key in RESOURCE_KEYS}
    model = ru.get("model") or "unknown"
    models = [item for item in ru.get("models", []) if isinstance(item, dict)]
    return {
        **result,
        "scope": ru.get("usage_scope") or "unknown",
        "usage_scope": ru.get("usage_scope") or "unknown",
        "model": model,
        "models": models,
        "collection_status": collection_status,
        "unavailable_reason": unavailable_reason,
        "source": ru.get("source") or "unknown",
        "attribution": ru.get("attribution") or "unknown",
        "contamination_risk": ru.get("contamination_risk") or "unknown",
        "confidence": ru.get("confidence") or "unknown",
    }


def _code_delta(entry: dict) -> dict:
    worktree = entry.get("worktree_delta") if isinstance(entry.get("worktree_delta"), dict) else {}
    collection_status = str(worktree.get("collection_status") or "unavailable").strip()
    collected = collection_status == "collected"
    field_map = {
        "files_changed": "files_changed",
        "lines_added": "lines_added",
        "lines_deleted": "lines_deleted",
        "prod_files_changed": "production_files_changed",
        "prod_lines_added": "production_lines_added",
        "prod_lines_deleted": "production_lines_deleted",
        "test_files_changed": "test_files_changed",
        "test_lines_added": "test_lines_added",
        "test_lines_deleted": "test_lines_deleted",
        "docs_files_changed": "docs_files_changed",
        "docs_lines_added": "docs_lines_added",
        "docs_lines_deleted": "docs_lines_deleted",
        "other_files_changed": "other_files_changed",
        "other_lines_added": "other_lines_added",
        "other_lines_deleted": "other_lines_deleted",
        "binary_files_changed": "binary_files_changed",
    }
    return {
        **{
            target: _to_int_or_none(worktree.get(source)) if collected else None
            for target, source in field_map.items()
        },
        "scope": "stage_worktree" if collected else "unknown",
        "base_ref": None,
        "head_ref": None,
        "path_filter": "changed_files" if collected else "unknown",
        "source": worktree.get("source") or "unknown",
        "attribution": worktree.get("attribution") or "unknown",
        "contamination_risk": "unknown",
        "confidence": worktree.get("confidence") or "unknown",
        "collection_status": collection_status,
        "unavailable_reason": worktree.get("unavailable_reason"),
    }


def _sum_numeric_dicts(items: list[dict], keys: list[str]) -> dict:
    totals: dict[str, int | None] = {}
    for key in keys:
        values = [_to_int_or_none(item.get(key)) for item in items]
        numeric = [v for v in values if v is not None]
        totals[key] = sum(numeric) if numeric else None
    return totals


def _max_risk(values: list[Any]) -> str:
    risks = [str(v or "unknown") for v in values]
    return max(risks or ["unknown"], key=lambda v: RISK_ORDER.get(v, 0))


def _mixed_or_single(values: list[Any], default: str = "unknown") -> str:
    normalized = {str(v) for v in values if v not in {None, ""}}
    if not normalized:
        return default
    if len(normalized) == 1:
        return next(iter(normalized))
    return "mixed"


def _sum_resource_usage(entries: list[dict]) -> dict:
    return _aggregate_resource_usages([_resource_usage(e) for e in entries])


def _resource_unavailable_reason(usage: dict) -> str:
    reason = str(usage.get("unavailable_reason") or "").strip()
    if reason:
        return reason
    if usage.get("collection_status") == "pending":
        return "resource_usage_pending"
    if _to_int_or_none(usage.get("total_tokens")) is None:
        return "missing_total_tokens"
    return "resource_usage_unavailable"


def _lowest_confidence(usages: list[dict]) -> str:
    values = {str(usage.get("confidence") or "unknown") for usage in usages}
    if "low" in values:
        return "low"
    if "unknown" in values:
        return "unknown"
    if "medium" in values:
        return "medium"
    if "high" in values:
        return "high"
    return "unknown"


def _aggregate_resource_usages(usages: list[dict], scope: str = "aggregate") -> dict:
    if usages and any("eligible_entries" in usage for usage in usages):
        eligible_count = sum(int(usage.get("eligible_entries", 0) or 0) for usage in usages)
        collected_count = sum(int(usage.get("collected_entries", 0) or 0) for usage in usages)
        observed = {
            key: (
                sum(
                    _to_int_or_none((usage.get("observed_subtotal") or {}).get(key))
                    or _to_int_or_none(usage.get(key))
                    or 0
                    for usage in usages
                )
                if any(
                    _to_int_or_none((usage.get("observed_subtotal") or {}).get(key)) is not None
                    or _to_int_or_none(usage.get(key)) is not None
                    for usage in usages
                )
                else None
            )
            for key in RESOURCE_KEYS
        }
        exact = bool(eligible_count) and collected_count == eligible_count
        exact_totals = {key: observed[key] if exact else None for key in RESOURCE_KEYS}
        reasons = sorted({
            str(reason)
            for usage in usages
            for reason in (usage.get("missing_reasons") or [])
            if reason
        })
        return {
            **observed,
            "observed_subtotal": observed,
            "exact_total": exact_totals,
            "exact_total_tokens": exact_totals.get("total_tokens"),
            "eligible_entries": eligible_count,
            "collected_entries": collected_count,
            "missing_entries": max(eligible_count - collected_count, 0),
            "missing_reasons": reasons,
            "exact": exact,
            "scope": scope,
            "model": _mixed_or_single([usage.get("model") for usage in usages]),
            "collection_status": "collected" if exact else ("partial" if collected_count else "unavailable"),
            "unavailable_reason": (
                reasons[0]
                if len(reasons) == 1
                else ("multiple_resource_usage_failures" if reasons else None)
            ),
            "source": _mixed_or_single([usage.get("source") for usage in usages]),
            "attribution": _mixed_or_single([usage.get("attribution") for usage in usages]),
            "usage_scope": _mixed_or_single([usage.get("usage_scope") for usage in usages]),
            "contamination_risk": _max_risk([usage.get("contamination_risk") for usage in usages]),
            "confidence": _lowest_confidence([
                usage for usage in usages if int(usage.get("collected_entries", 0) or 0) > 0
            ]),
        }
    eligible = [
        usage
        for usage in usages
        if usage.get("collection_status") in {"pending", "collected", "unavailable"}
    ]
    collected = [
        usage
        for usage in eligible
        if usage.get("collection_status") == "collected"
        and _to_int_or_none(usage.get("total_tokens")) is not None
    ]
    observed = {
        key: (
            sum(_to_int_or_none(usage.get(key)) or 0 for usage in collected)
            if collected and any(_to_int_or_none(usage.get(key)) is not None for usage in collected)
            else None
        )
        for key in RESOURCE_KEYS
    }
    exact = bool(eligible) and len(collected) == len(eligible)
    exact_totals = {
        key: observed[key] if exact and all(_to_int_or_none(usage.get(key)) is not None for usage in collected) else None
        for key in RESOURCE_KEYS
    }
    incomplete = [usage for usage in eligible if usage not in collected]
    reasons = sorted({_resource_unavailable_reason(usage) for usage in incomplete})
    unavailable_reason = reasons[0] if len(reasons) == 1 else ("multiple_resource_usage_failures" if reasons else None)
    if exact:
        collection_status = "collected"
    elif collected:
        collection_status = "partial"
    else:
        collection_status = "unavailable"
    confidence = _lowest_confidence(collected) if collected else "low"
    return {
        **observed,
        "observed_subtotal": observed,
        "exact_total": exact_totals,
        "exact_total_tokens": exact_totals.get("total_tokens"),
        "eligible_entries": len(eligible),
        "collected_entries": len(collected),
        "missing_entries": max(len(eligible) - len(collected), 0),
        "missing_reasons": reasons,
        "exact": exact,
        "scope": scope if eligible else "unknown",
        "model": next((u.get("model") for u in reversed(usages) if u.get("model") and u.get("model") != "unknown"), "unknown"),
        "collection_status": collection_status,
        "unavailable_reason": unavailable_reason,
        "source": _mixed_or_single([u.get("source") for u in collected]),
        "attribution": _mixed_or_single([u.get("attribution") for u in usages]),
        "usage_scope": _mixed_or_single([u.get("usage_scope") for u in usages]),
        "contamination_risk": _max_risk([u.get("contamination_risk") for u in usages]),
        "confidence": confidence,
    }


def _sum_code_delta(entries: list[dict]) -> dict:
    deltas = [_code_delta(e) for e in entries]
    observed = [
        delta
        for delta in deltas
        if delta.get("collection_status") == "collected"
        and any(_to_int_or_none(delta.get(key)) is not None for key in CODE_DELTA_KEYS)
    ]
    if not observed:
        return _unknown_code_delta()
    totals = _sum_numeric_dicts(observed, CODE_DELTA_KEYS)
    confidences = {d.get("confidence") for d in observed if d.get("confidence") not in {"unknown", None}}
    return {
        **totals,
        "scope": "feature_worktree",
        "base_ref": None,
        "head_ref": None,
        "path_filter": _mixed_or_single([d.get("path_filter") for d in observed]),
        "source": _mixed_or_single([d.get("source") for d in observed]),
        "attribution": _mixed_or_single([d.get("attribution") for d in observed]),
        "contamination_risk": "unknown",
        "confidence": "low" if "low" in confidences else ("medium" if "medium" in confidences else ("high" if "high" in confidences else "unknown")),
        "collection_status": "collected",
        "unavailable_reason": None,
    }


def _unknown_code_delta() -> dict:
    return {
        **{key: None for key in CODE_DELTA_KEYS},
        "scope": "unknown",
        "base_ref": None,
        "head_ref": None,
        "path_filter": "unknown",
        "source": "unknown",
        "attribution": "unknown",
        "contamination_risk": "unknown",
        "confidence": "unknown",
        "collection_status": "unavailable",
        "unavailable_reason": "worktree_delta_unavailable",
    }


def _fmt_tokens(value: Any) -> str:
    n = _to_int_or_none(value)
    if n is None:
        return "—"
    if abs(n) >= 100_000_000:
        value_in_yi = f"{n / 100_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{value_in_yi}亿"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_plain_num(value: Any) -> str:
    n = _to_int_or_none(value)
    return "—" if n is None else f"{n:,}"


def _fmt_code_delta(delta: dict) -> str:
    added = _to_int_or_none(delta.get("lines_added"))
    deleted = _to_int_or_none(delta.get("lines_deleted"))
    if added is None and deleted is None:
        return "—"
    return f"+{added or 0} / -{deleted or 0}"


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_log_time(values: list[Any]) -> str:
    """Compare recorded timestamps in UTC; naive display timestamps are interpreted as UTC."""
    times = [parsed for value in values if (parsed := _parse_time(value)) is not None]
    return max(
        (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
         for value in times),
    ).isoformat() if times else ""


def _newest_log_sort_key(value: Any) -> float:
    """Sort actual log times newest first, with missing or invalid times last."""
    parsed = _parse_time(_latest_log_time([value]))
    return -parsed.timestamp() if parsed is not None else float("inf")


def _beijing_time(value: Any) -> datetime | None:
    """Convert ledger timestamps for dashboard display."""
    parsed = _parse_time(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=8)))


def _format_beijing_time(value: Any, *, seconds: bool = False) -> str:
    local = _beijing_time(value)
    return local.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M") if local else "未记录"


def _log_time_html(value: str, *, latest: bool = False) -> str:
    local = _beijing_time(value)
    prefix = "最新日志 " if latest else ""
    if local is None:
        return f'<span class="helixharness-log-time">{prefix}未采集</span>'
    return (
        f'<span class="helixharness-log-time">{prefix}'
        f'<time datetime="{_esc(value)}" title="日志实际时间（北京时间）">'
        f'<span class="helixharness-log-date">{local:%Y-%m-%d}</span> '
        f'<span class="helixharness-log-clock">{local:%H:%M:%S}</span></time></span>'
    )


def _duration_sec(entry: dict) -> int | None:
    direct = _to_int_or_none(entry.get("duration_sec"))
    if direct is not None:
        return max(direct, 0)
    started = _parse_time(entry.get("started_at"))
    ended = _parse_time(entry.get("ended_at"))
    if started and ended:
        return max(round((ended - started).total_seconds()), 0)
    return None


def _archived_duration_average(features: list[dict]) -> tuple[int | None, int, int]:
    """Return average seconds, observed sample count and undated archived count.

    Preserve the existing cumulative stage duration, including repeated attempts.
    A measured zero is a valid sample; an entirely missing duration is not.
    """
    archived = [feature for feature in features if feature.get("status") == "archived"]
    samples = [
        feature for feature in archived
        if any(_duration_sec(entry) is not None for entry in feature.get("entries", []))
    ]
    total = sum(int(feature.get("total_duration_sec") or 0) for feature in samples)
    return (round(total / len(samples)) if samples else None, len(samples), len(archived) - len(samples))


def _fmt_duration(seconds: Any) -> str:
    sec = _to_int_or_none(seconds)
    if sec is None:
        return "—"
    sec = max(sec, 0)
    hours, rem = divmod(sec, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _fmt_duration_hours(seconds: Any) -> str:
    sec = _to_int_or_none(seconds)
    if sec is None:
        return "—"
    return f"{max(sec, 0) / 3600:.2f}h"


def _resource_summary_line(usage: dict) -> str:
    observed = _to_int_or_none(usage.get("total_tokens"))
    eligible = int(usage.get("eligible_entries", 0) or 0)
    collected = int(usage.get("collected_entries", 0) or 0)
    if observed is None:
        return "Token 未采集"
    exact = "完整准确" if usage.get("exact") else f"准确覆盖 {collected}/{eligible}"
    return f'已采集 {_fmt_tokens(observed)} · {exact}'


def _code_summary_line(delta: dict) -> str:
    if all(_to_int_or_none(delta.get(k)) is None for k in ["lines_added", "lines_deleted", "files_changed"]):
        return ARCHIVE_REASONS.get(delta.get("unavailable_reason"), "代码变更未采集")
    return (
        (f'{_code_delta_label(delta)} · ' if delta.get("scope") in {"feature_archive", "archive_review"} else "") +
        f'共变更 {_fmt_plain_num(delta.get("files_changed"))} 个文件 · '
        f'生产代码 +{_fmt_plain_num(delta.get("prod_lines_added"))}/-{_fmt_plain_num(delta.get("prod_lines_deleted"))} · '
        f'测试代码 +{_fmt_plain_num(delta.get("test_lines_added"))}/-{_fmt_plain_num(delta.get("test_lines_deleted"))} · '
        f'文档 +{_fmt_plain_num(delta.get("docs_lines_added"))}/-{_fmt_plain_num(delta.get("docs_lines_deleted"))}'
    )


def _code_summary_html(delta: dict) -> str:
    if all(_to_int_or_none(delta.get(k)) is None for k in ["lines_added", "lines_deleted", "files_changed"]):
        return '<span>代码变更未采集</span>'
    lines = [
        f'共变更 {_fmt_plain_num(delta.get("files_changed"))} 个文件',
        f'生产代码 +{_fmt_plain_num(delta.get("prod_lines_added"))} / -{_fmt_plain_num(delta.get("prod_lines_deleted"))}',
        f'测试代码 +{_fmt_plain_num(delta.get("test_lines_added"))} / -{_fmt_plain_num(delta.get("test_lines_deleted"))}',
        f'文档 +{_fmt_plain_num(delta.get("docs_lines_added"))} / -{_fmt_plain_num(delta.get("docs_lines_deleted"))}',
    ]
    return "<br>".join(f'<span>{_esc(line)}</span>' for line in lines)


def _entry_interventions(entry: dict) -> int:
    return _friction(entry)["interventions"]


def _entry_user_turns(entry: dict) -> int | None:
    """Return collected user turns, preserving missing data and measured zero."""
    collection = entry.get("collection") if isinstance(entry.get("collection"), dict) else {}
    raw = collection.get("user_turn_count")
    if not isinstance(raw, (int, str)) or isinstance(raw, bool) or entry.get("telemetry_status") == "pending":
        return None
    value = _to_int_or_none(raw)
    return value if value is not None and value >= 0 else None


def _user_interactions(entries: list[dict]) -> dict:
    """Sum physical stage windows; this does not classify effective interventions."""
    values = [value for entry in entries if (value := _entry_user_turns(entry)) is not None]
    return {
        "total": sum(values) if values else None,
        "collected_entries": len(values),
        "eligible_entries": len(entries),
        "exact": bool(entries) and len(values) == len(entries),
    }


def _merge_user_interactions(summaries: list[dict]) -> dict:
    collected = sum(int(item.get("collected_entries") or 0) for item in summaries)
    eligible = sum(int(item.get("eligible_entries") or 0) for item in summaries)
    return {
        "total": sum(int(item.get("total") or 0) for item in summaries) if collected else None,
        "collected_entries": collected,
        "eligible_entries": eligible,
        "exact": bool(eligible) and collected == eligible,
    }


def _user_interaction_value(summary: dict | None) -> str:
    value = (summary or {}).get("total")
    return str(value) if value is not None else "未采集"


def _user_interaction_hint(summary: dict | None) -> str:
    summary = summary or {}
    collected, eligible = summary.get("collected_entries", 0), summary.get("eligible_entries", 0)
    label = "已采集小计" if collected and not summary.get("exact") else "已采集"
    return f"{label} · {collected}/{eligible} 条阶段记录"


def _entry_breakdown(entry: dict) -> dict[str, int]:
    return _friction(entry)["breakdown"]


def _invocation(entry: dict) -> dict:
    return entry.get("invocation") if isinstance(entry.get("invocation"), dict) else {}


def _entry_platform(entry: dict) -> str:
    return str(entry.get("platform") or _invocation(entry).get("platform") or "").strip().lower()


def _entry_trigger(entry: dict) -> str:
    """Return the fixed-ledger or materialized invocation trigger."""

    return str(entry.get("trigger") or _invocation(entry).get("trigger") or "").strip()


def _bug_fix_count(entries: list[dict]) -> int:
    """Count Bug Fix workflow runs without counting each stage separately."""

    starts = [
        entry
        for entry in entries
        if _entry_trigger(entry) == "bug-fix" and entry.get("command") == "propose"
    ]
    if starts:
        attempts = {
            entry.get("attempt")
            if isinstance(entry.get("attempt"), int) and not isinstance(entry.get("attempt"), bool)
            else entry.get("entry_id")
            for entry in starts
        }
        return len(attempts)
    return int(any(_entry_trigger(entry) == "bug-fix" for entry in entries))


def _loop_enabled(entry: dict) -> bool:
    inv = _invocation(entry)
    return inv.get("loop") is True or inv.get("mode") == "loop"


def _loop_rate(count: int, total: int) -> str:
    if not total:
        return "—"
    return f"{round(count / total * 100)}%"


def _loop_usage_from_feature_summary(fs: dict, entries: list[dict]) -> dict:
    lu = fs.get("loop_usage") if isinstance(fs.get("loop_usage"), dict) else {}
    enabled = lu.get("enabled")
    if enabled is None:
        enabled = any(_loop_enabled(e) for e in entries)
    return {
        "enabled": bool(enabled),
        "max_turns": _to_int_or_none(lu.get("max_turns")),
    }


def _loop_feature_summary(loop_features: int, total_features: int, loop_success: int, loop_turns: int) -> str:
    if not total_features:
        return "暂无需求"
    success = "—" if not loop_features else _loop_rate(loop_success, loop_features)
    avg_turns = "—" if not loop_features else round(loop_turns / loop_features, 1)
    return f"覆盖 {_loop_rate(loop_features, total_features)} · 成功 {success} · 平均轮次 {avg_turns}"


def _loop_hint(row: dict) -> str:
    return _loop_feature_summary(
        int(row.get("loop_count", 0) or 0),
        int(row.get("loop_total", 0) or 0),
        int(row.get("loop_success", 0) or 0),
        int(row.get("loop_turns", 0) or 0),
    )


def _pending_status_count(entries: list[dict]) -> int:
    return sum(1 for e in entries if e.get("telemetry_status") == "pending")


def _collection_state(eval_log: dict) -> dict:
    state = eval_log.get("collection_state") if isinstance(eval_log.get("collection_state"), dict) else {}
    coverage = str(state.get("coverage") or "unknown").strip().lower()
    if coverage not in {"observed_from_start", "partial"}:
        coverage = "unknown"
    missing = state.get("pre_enable_stages_missing")
    if not isinstance(missing, list):
        missing = []
    return {
        "coverage": coverage,
        "reason": state.get("reason"),
        "collection_started_at": state.get("collection_started_at"),
        "pre_enable_stages_missing": [str(item) for item in missing if item],
        "first_observed_event": state.get("first_observed_event")
        if isinstance(state.get("first_observed_event"), dict)
        else {},
        "sealed_at": state.get("sealed_at"),
        "sealed_by_entry_id": state.get("sealed_by_entry_id"),
    }


def _feature_summary(eval_log: dict) -> dict:
    entries = eval_log.get("entries", [])
    breakdown = {k: 0 for k in BREAKDOWN_KEYS}
    for e in entries:
        eb = _entry_breakdown(e)
        for k in BREAKDOWN_KEYS:
            breakdown[k] += eb.get(k, 0)
    entries_by_stage: dict[str, list[dict]] = {stage: [] for stage in STAGE_ORDER}
    for entry in entries:
        stage = entry.get("stage_kind")
        if stage in entries_by_stage:
            entries_by_stage[str(stage)].append(entry)
    expected_stages = STAGE_ORDER
    stage_coverage = {stage: bool(entries_by_stage[stage]) for stage in expected_stages}
    completed_stages = sum(
        1
        for stage, stage_entries in entries_by_stage.items()
        if stage in expected_stages
        if any(entry.get("status") == "completed" for entry in stage_entries)
    )
    review_findings = [
        _quality(entry)["review_findings_count"]
        for entry in entries
        if entry.get("stage_kind") == "coding" and _quality(entry)["review_findings_count"] is not None
    ]
    verify_passed = _latest_non_null(entries, lambda entry: _quality(entry)["verify_passed"])
    evidence_complete = _latest_non_null(entries, lambda entry: _quality(entry)["evidence_complete"])
    collection_state = _collection_state(eval_log)
    pending_entries = _pending_status_count(entries)
    finalization = eval_log.get("finalization") if isinstance(eval_log.get("finalization"), dict) else {}
    loop_max_turns = _latest_non_null(
        entries, lambda entry: _to_int_or_none(_invocation(entry).get("loop_max_turns"))
    )
    archive_entry_completed = any(
        entry.get("stage_kind") == "archive" and entry.get("status") == "completed"
        for entry in entries
    )
    archive_completed = bool(
        eval_log.get("status") == "archived"
        and collection_state.get("sealed_at")
        and collection_state.get("sealed_by_entry_id")
        and archive_entry_completed
    )
    workflow_completed = bool(
        collection_state["coverage"] == "observed_from_start"
        and archive_completed
        and completed_stages == len(expected_stages)
    )
    telemetry_finalized = finalization.get("status") in {"complete", "complete_with_unavailable"}
    semantic_candidates = sum(_collection(entry)["candidate_user_turn_count"] for entry in entries)
    semantic_classified = sum(_collection(entry)["classified_user_turn_count"] for entry in entries)
    semantic_events = sum(_friction(entry)["semantic_interventions"] for entry in entries)
    deterministic_events = sum(_friction(entry)["deterministic_interventions"] for entry in entries)
    semantic_statuses = sorted({_collection(entry)["semantic_status"] for entry in entries})
    started = [_parse_time(entry.get("started_at")) for entry in entries]
    ended = [_parse_time(entry.get("ended_at")) for entry in entries]
    started = [value for value in started if value]
    ended = [value for value in ended if value]
    qualified_lead_time_sec = None
    if workflow_completed and telemetry_finalized and started and ended:
        qualified_lead_time_sec = max(round((max(ended) - min(started)).total_seconds()), 0)
    return {
        "total_entries": len(entries),
        "observed_stage_duration_sum_sec": sum((_duration_sec(entry) or 0) for entry in entries),
        "qualified_lead_time_sec": qualified_lead_time_sec,
        "total_interventions": sum(_entry_interventions(e) for e in entries),
        "deterministic_intervention_subtotal": deterministic_events,
        "semantic_intervention_subtotal": semantic_events,
        "semantic_candidate_count": semantic_candidates,
        "semantic_classified_count": semantic_classified,
        "semantic_statuses": semantic_statuses,
        "intervention_breakdown": breakdown,
        "rework_entries": sum(1 for e in entries if _friction(e)["rework"] is True),
        "rollback_entries": sum(1 for e in entries if e.get("stage_kind") == "rollback"),
        "review_findings_count": sum(int(value or 0) for value in review_findings) if review_findings else None,
        "verify_passed": verify_passed,
        "evidence_complete": evidence_complete,
        "archive_completed": archive_completed,
        "workflow_completed": workflow_completed,
        "telemetry_finalized": telemetry_finalized,
        "finalization_status": finalization.get("status") or "pending",
        "finalization_error_code": finalization.get("error_code"),
        "finalization_retryable": bool(finalization.get("retryable")),
        "unavailable_metrics": finalization.get("unavailable_metrics")
        if isinstance(finalization.get("unavailable_metrics"), list)
        else [],
        "workflow_completion_rate": round(completed_stages / len(expected_stages), 2) if expected_stages else 0,
        "pending_entries": pending_entries,
        "stage_coverage": stage_coverage,
        "expected_stage_count": len(expected_stages),
        "loop_usage": {
            "enabled": any(_loop_enabled(entry) for entry in entries),
            "max_turns": loop_max_turns,
        },
    }


def _summary_resource_usage(fs: dict, entries: list[dict]) -> dict:
    return _sum_resource_usage(entries)


def _summary_code_delta(entries: list[dict]) -> dict:
    return _sum_code_delta(entries)


def _latest_non_null(entries: list[dict], getter):
    for e in reversed(entries):
        value = getter(e)
        if value is not None:
            return value
    return None


def aggregate_feature(eval_log: dict, fallback_root: str = "") -> dict:
    entries = [dict(entry) for entry in eval_log.get("entries", []) if isinstance(entry, dict)]
    project = _project(eval_log, fallback_root)
    derived_log = {**eval_log, "entries": entries}
    fs = _feature_summary(derived_log)
    breakdown = {k: int(fs.get("intervention_breakdown", {}).get(k, 0) or 0) for k in BREAKDOWN_KEYS}
    total_interventions = int(fs.get("total_interventions") if fs.get("total_interventions") is not None else sum(_entry_interventions(e) for e in entries))
    revision_entries = [_workflow(e) for e in entries if _workflow(e)["revises_previous_stage"]]
    revision_breakdown: dict[str, int] = {}
    revision_reasons: dict[str, int] = {}
    for wf in revision_entries:
        stage = wf.get("revised_stage_kind") or "unknown"
        reason = wf.get("revision_reason") or "unknown"
        revision_breakdown[stage] = revision_breakdown.get(stage, 0) + 1
        revision_reasons[reason] = revision_reasons.get(reason, 0) + 1

    entries_by_stage: dict[str, list[dict]] = {stage: [] for stage in STAGE_ORDER}
    for e in entries:
        sk = e.get("stage_kind")
        if sk in entries_by_stage:
            entries_by_stage[sk].append(e)
    attempt_metadata = _execution_attempt_metadata(entries)

    stage_timeline = []
    timeline_stages = STAGE_ORDER
    for sk in timeline_stages:
        stage_entries = entries_by_stage[sk]
        if stage_entries:
            latest = stage_entries[-1]
            stage_attempts = [
                attempt_metadata.get(str(entry.get("entry_id") or ""), {})
                for entry in stage_entries
            ]
            attempt_count = sum(1 for attempt in stage_attempts if attempt.get("segment_index") == 1)
            stage_timeline.append({
                "stage_kind": sk,
                "entry_count": len(stage_entries),
                "attempt_count": attempt_count,
                "continuation_segment_count": max(len(stage_entries) - attempt_count, 0),
                "interventions": sum(_entry_interventions(entry) for entry in stage_entries),
                "user_interactions": _user_interactions(stage_entries),
                "started_at": stage_entries[0].get("started_at", ""),
                "ended_at": latest.get("ended_at", ""),
                "duration_sec": sum((_duration_sec(entry) or 0) for entry in stage_entries),
                "status": latest.get("status", "completed"),
                "collection_status": _mixed_or_single(
                    [_resource_usage(entry).get("collection_status") for entry in stage_entries]
                ),
            })
        else:
            stage_timeline.append({
                "stage_kind": sk,
                "entry_count": 0,
                "attempt_count": 0,
                "continuation_segment_count": 0,
                "interventions": None,
                "user_interactions": _user_interactions([]),
                "ended_at": None,
                "duration_sec": None,
                "status": "pending",
                "collection_status": "none",
            })

    current_stage = next(
        (str(entry.get("stage_kind")) for entry in reversed(entries) if entry.get("stage_kind") in STAGE_ORDER),
        "requirement",
    )
    latest_stage_status = next(
        (str(entry.get("status")) for entry in reversed(entries) if entry.get("stage_kind") in STAGE_ORDER),
        "pending",
    )
    bug_fix_count = _bug_fix_count(entries)
    quality_verify = fs.get("verify_passed")
    if quality_verify is None:
        quality_verify = _latest_non_null(entries, lambda e: _quality(e)["verify_passed"])
    evidence = fs.get("evidence_complete")
    if evidence is None:
        evidence = _latest_non_null(entries, lambda e: _quality(e)["evidence_complete"])

    review_findings = fs.get("review_findings_count")
    if review_findings is None:
        vals = [_quality(e)["review_findings_count"] for e in entries if _quality(e)["review_findings_count"] is not None]
        review_findings = sum(int(v or 0) for v in vals) if vals else None

    users_by_name: dict[str, dict] = {}
    for entry in entries:
        actor = _actor(entry)
        if _is_countable_user(actor["user_name"]):
            users_by_name[actor["user_name"]] = actor
    installations = sorted(users_by_name.values(), key=lambda item: item["user_name"])
    actors = [item["user_name"] for item in installations]
    collection_scopes = sorted({_collection(e)["scope"] for e in entries if _collection(e)["scope"] != "none"})
    last_updated = entries[-1].get("ended_at", "") if entries else eval_log.get("last_updated", "")
    log_time = _latest_log_time([
        value for entry in entries for value in (entry.get("ended_at"), entry.get("started_at"))
    ]) or _latest_log_time([eval_log.get("last_updated"), eval_log.get("created_at")])

    per_command: dict[str, dict] = {c: {"count": 0, "interventions": 0} for c in COMMANDS}
    for e in entries:
        cmd = e.get("command", "")
        if cmd in per_command:
            per_command[cmd]["count"] += 1
            per_command[cmd]["interventions"] += _entry_interventions(e)

    resource_usage = _summary_resource_usage(fs, entries)
    code_delta = _summary_code_delta(entries)
    archive_changes = archive_code_delta(eval_log, fallback_root) if eval_log.get("schema") == LEDGER_SCHEMA else {}
    if code_delta.get("collection_status") != "collected" and archive_changes:
        code_delta = {**code_delta, **archive_changes}
    loop_usage = _loop_usage_from_feature_summary(fs, entries)
    collection_state = _collection_state(eval_log)
    workflow_completed = bool(fs.get("workflow_completed"))
    workflow_completion_rate = float(fs.get("workflow_completion_rate") or 0)
    pending_entries = _to_int_or_none(fs.get("pending_entries"))
    if pending_entries is None:
        pending_entries = _pending_status_count(entries)
    versions = [
        str(entry.get("helixharness_version"))
        for entry in entries
        if entry.get("helixharness_version")
    ]
    platforms = [_entry_platform(entry) for entry in entries if _entry_platform(entry)]

    return {
        "feature": _feature_name(eval_log.get("feature"), "?"),
        "repository": os.path.basename(os.path.normpath(fallback_root)) if fallback_root else "",
        "feature_identity": os.path.basename(os.path.dirname(eval_log["_log_path"])) if eval_log.get("_log_path") else _feature_name(eval_log.get("feature"), "?"),
        "project_name": project["project_name"],
        "project_id": project["project_id"],
        "archive_changes": archive_changes,
        "project": project,
        "status": eval_log.get("status", "in_progress"),
        "current_stage": current_stage,
        "latest_stage_status": latest_stage_status,
        "bug_fix_count": bug_fix_count,
        "total_interventions": total_interventions,
        "user_interactions": _user_interactions(entries),
        "deterministic_interventions": int(fs.get("deterministic_intervention_subtotal") or 0),
        "semantic_interventions": int(fs.get("semantic_intervention_subtotal") or 0),
        "semantic_candidate_count": int(fs.get("semantic_candidate_count") or 0),
        "semantic_classified_count": int(fs.get("semantic_classified_count") or 0),
        "semantic_statuses": fs.get("semantic_statuses") or [],
        "total_duration_sec": _to_int_or_none(fs.get("observed_stage_duration_sum_sec"))
        or sum((_duration_sec(e) or 0) for e in entries),
        "qualified_lead_time_sec": _to_int_or_none(fs.get("qualified_lead_time_sec")),
        "intervention_breakdown": breakdown,
        "per_command": per_command,
        "stage_timeline": stage_timeline,
        "rework_count": int(
            fs.get("rework_entries")
            or sum(1 for e in entries if _friction(e)["rework"] is True)
        ),
        "revision_count": len(revision_entries),
        "revision_breakdown": revision_breakdown,
        "revision_reasons": revision_reasons,
        "rollback_count": int(fs.get("rollback_entries") or sum(1 for e in entries if e.get("stage_kind") == "rollback")),
        "command_count": len(entries),
        "last_updated": last_updated,
        "log_time": log_time,
        "review_findings_count": review_findings,
        "verify_passed": quality_verify,
        "evidence_complete": evidence,
        "archive_completed": bool(fs.get("archive_completed")),
        "telemetry_finalized": bool(fs.get("telemetry_finalized")),
        "finalization_status": fs.get("finalization_status") or "pending",
        "finalization_error_code": fs.get("finalization_error_code"),
        "finalization_retryable": bool(fs.get("finalization_retryable")),
        "unavailable_metrics": fs.get("unavailable_metrics") or [],
        "resource_usage": resource_usage,
        "code_delta": code_delta,
        "loop_count": 1 if loop_usage["enabled"] else 0,
        "loop_total": 1,
        "loop_usage": loop_usage,
        "pending_entries": int(pending_entries or 0),
        "workflow_completed": bool(workflow_completed),
        "workflow_completion_rate": float(workflow_completion_rate or 0),
        "stage_coverage": fs.get("stage_coverage") or {stage: False for stage in STAGE_ORDER},
        "expected_stage_count": int(fs.get("expected_stage_count") or len(STAGE_ORDER)),
        "collection_state": collection_state,
        "collection_coverage": collection_state["coverage"],
        "actors": actors,
        "installations": installations,
        "collection_scopes": collection_scopes,
        "version_first": versions[0] if versions else "unknown",
        "version_latest": versions[-1] if versions else "unknown",
        "version_set": sorted(set(versions)),
        "platform_set": sorted(set(platforms)),
        "entries": entries,
    }


def aggregate_all(projects: list[dict]) -> dict:
    """Aggregate current fixed-ledger and Supplement telemetry."""
    features = [
        aggregate_feature(log, project.get("project_dir", ""))
        for project in projects
        for log in project.get("features", [])
    ]
    return _aggregate_features(projects, features)


def _aggregate_features(projects: list[dict], features: list[dict]) -> dict:
    """Build views from already normalized features without repeating Git reads."""
    user_projects: dict[tuple[str, str], dict] = {}
    project_rows: dict[str, dict] = {}

    for feature in features:
        project_id = feature["project_id"]
        project_row = project_rows.setdefault(project_id, {
            "project_id": project_id,
            "project_name": feature["project_name"],
            "feature_keys": set(),
            "user_names": set(),
            "archived": 0,
            "workflow_completed": 0,
            "telemetry_finalized": 0,
            "verify_passed": 0,
            "verify_total": 0,
            "evidence_complete": 0,
            "evidence_total": 0,
            "interventions": 0,
            "deterministic_interventions": 0,
            "semantic_interventions": 0,
            "rework": 0,
            "revisions": 0,
            "findings": 0,
            "pending_entries": 0,
            "collection_observed_from_start": 0,
            "collection_partial": 0,
            "collection_unknown": 0,
            "entry_usages": [],
            "interaction_usages": [],
        })
        feature_key = f'{project_id}::{feature["feature"]}'
        project_row["feature_keys"].add(feature_key)
        project_row["archived"] += int(feature["archive_completed"])
        project_row["workflow_completed"] += int(feature["workflow_completed"])
        project_row["telemetry_finalized"] += int(feature["telemetry_finalized"])
        project_row["interventions"] += int(feature["total_interventions"])
        project_row["deterministic_interventions"] += int(feature["deterministic_interventions"])
        project_row["semantic_interventions"] += int(feature["semantic_interventions"])
        project_row["rework"] += int(feature["rework_count"])
        project_row["revisions"] += int(feature["revision_count"])
        project_row["findings"] += int(feature["review_findings_count"] or 0)
        project_row["pending_entries"] += int(feature["pending_entries"])
        coverage_key = {
            "observed_from_start": "collection_observed_from_start",
            "partial": "collection_partial",
        }.get(feature["collection_coverage"], "collection_unknown")
        project_row[coverage_key] += 1
        if feature["verify_passed"] is not None:
            project_row["verify_total"] += 1
            project_row["verify_passed"] += int(feature["verify_passed"] is True)
        if feature["evidence_complete"] is not None:
            project_row["evidence_total"] += 1
            project_row["evidence_complete"] += int(feature["evidence_complete"] is True)
        project_row["entry_usages"].extend(_resource_usage(entry) for entry in feature["entries"])
        project_row["interaction_usages"].append(feature["user_interactions"])

        entries_by_user: dict[str, list[dict]] = {}
        user_meta = {
            item["user_name"]: item
            for item in feature.get("installations", [])
            if _is_countable_user(item.get("user_name"))
        }
        for entry in feature["entries"]:
            actor = _actor(entry)
            if _is_countable_user(actor["user_name"]):
                entries_by_user.setdefault(actor["user_name"], []).append(entry)
        for user_name, actor_entries in entries_by_user.items():
            actor = user_meta.get(user_name) or _actor(actor_entries[-1])
            project_row["user_names"].add(user_name)
            key = (user_name, project_id)
            row = user_projects.setdefault(key, {
                "user_id": user_name,
                "user_name": actor["user_name"],
                "source": actor["source"],
                "confidence": actor["confidence"],
                "project_id": project_id,
                "project_name": feature["project_name"],
                "features": set(),
                "archived_features": set(),
                "workflow_completed": 0,
                "telemetry_finalized": 0,
                "verify_passed": 0,
                "verify_total": 0,
                "evidence_complete": 0,
                "evidence_total": 0,
                "interventions": 0,
                "deterministic_interventions": 0,
                "semantic_interventions": 0,
                "intervention_breakdown": {name: 0 for name in BREAKDOWN_KEYS},
                "rework": 0,
                "revisions": 0,
                "rollback": 0,
                "findings": 0,
                "loop_count": 0,
                "loop_total": 0,
                "pending_entries": 0,
                "entry_usages": [],
                "interaction_usages": [],
            })
            first_feature_for_user = feature_key not in row["features"]
            row["features"].add(feature_key)
            if feature["archive_completed"]:
                row["archived_features"].add(feature_key)
            if first_feature_for_user:
                row["workflow_completed"] += int(feature["workflow_completed"])
                row["telemetry_finalized"] += int(feature["telemetry_finalized"])
                row["loop_count"] += int(feature["loop_count"])
                row["loop_total"] += 1
                if feature["verify_passed"] is not None:
                    row["verify_total"] += 1
                    row["verify_passed"] += int(feature["verify_passed"] is True)
                if feature["evidence_complete"] is not None:
                    row["evidence_total"] += 1
                    row["evidence_complete"] += int(feature["evidence_complete"] is True)
            row["interaction_usages"].append(_user_interactions(actor_entries))
            for entry in actor_entries:
                friction = _friction(entry)
                quality = _quality(entry)
                workflow = _workflow(entry)
                row["interventions"] += friction["interventions"]
                row["deterministic_interventions"] += friction["deterministic_interventions"]
                row["semantic_interventions"] += friction["semantic_interventions"]
                for name in BREAKDOWN_KEYS:
                    row["intervention_breakdown"][name] += friction["breakdown"][name]
                row["rework"] += int(friction["rework"] is True)
                row["revisions"] += int(workflow["revises_previous_stage"])
                row["rollback"] += int(entry.get("stage_kind") == "rollback")
                row["findings"] += int(quality["review_findings_count"] or 0)
                row["pending_entries"] += int(entry.get("telemetry_status") == "pending")
                row["entry_usages"].append(_resource_usage(entry))

    user_project_rows = []
    for row in user_projects.values():
        feature_count = len(row["features"])
        user_project_rows.append({
            **{
                key: value
                for key, value in row.items()
                if key not in {"features", "archived_features", "entry_usages", "interaction_usages"}
            },
            "feature_count": feature_count,
            "archived_count": len(row["archived_features"]),
            "avg_interventions": round(row["interventions"] / feature_count, 1) if feature_count else 0,
            "resource_usage": _aggregate_resource_usages(row["entry_usages"], "user_project"),
            "user_interactions": _merge_user_interactions(row["interaction_usages"]),
            "code_delta": _unknown_code_delta(),
        })
    user_project_rows.sort(key=lambda row: (row["project_name"], row["user_id"]))

    project_view = []
    for row in project_rows.values():
        feature_count = len(row["feature_keys"])
        project_view.append({
            **{
                key: value
                for key, value in row.items()
                if key not in {"feature_keys", "user_names", "entry_usages", "interaction_usages"}
            },
            "feature_count": feature_count,
            "user_count": len(row["user_names"]),
            "avg_interventions": round(row["interventions"] / feature_count, 1) if feature_count else 0,
            "resource_usage": _aggregate_resource_usages(row["entry_usages"], "project"),
            "user_interactions": _merge_user_interactions(row["interaction_usages"]),
            "code_delta": _sum_numeric_dicts(
                [feature["code_delta"] for feature in features if feature["project_id"] == row["project_id"]],
                CODE_DELTA_KEYS,
            ),
        })
    project_view.sort(key=lambda row: row["project_name"])

    all_entry_usages = [
        _resource_usage(entry)
        for feature in features
        for entry in feature["entries"]
    ]
    resource_usage = _aggregate_resource_usages(all_entry_usages, "global")
    invalid_logs = [
        invalid
        for project in projects
        for invalid in project.get("invalid_logs", [])
    ]
    stage_counts = {
        stage: sum(int(feature["stage_coverage"].get(stage, False)) for feature in features)
        for stage in STAGE_ORDER
    }
    qualified_lead_times = [
        feature["qualified_lead_time_sec"]
        for feature in features
        if feature.get("qualified_lead_time_sec") is not None
    ]
    semantic_incomplete = sum(
        1
        for feature in features
        if any(status not in {"complete", "no_candidates"} for status in feature["semantic_statuses"])
    )
    total_interventions = sum(feature["total_interventions"] for feature in features)
    summary = {
        "project_total": len([project for project in projects if project.get("features")]),
        "user_total": len({row["user_id"] for row in user_project_rows}),
        "feature_total": len(features),
        "workflow_completed": sum(int(feature["workflow_completed"]) for feature in features),
        "archived": sum(int(feature["archive_completed"]) for feature in features),
        "telemetry_finalized": sum(int(feature["telemetry_finalized"]) for feature in features),
        "verify_passed": sum(int(feature["verify_passed"] is True) for feature in features),
        "verified": sum(int(feature["verify_passed"] is True) for feature in features),
        "verify_total": sum(int(feature["verify_passed"] is not None) for feature in features),
        "evidence_complete": sum(int(feature["evidence_complete"] is True) for feature in features),
        "evidence_total": sum(int(feature["evidence_complete"] is not None) for feature in features),
        "total_interventions": total_interventions,
        "user_interactions": _merge_user_interactions([feature["user_interactions"] for feature in features]),
        "deterministic_interventions": sum(feature["deterministic_interventions"] for feature in features),
        "semantic_interventions": sum(feature["semantic_interventions"] for feature in features),
        "semantic_candidate_count": sum(feature["semantic_candidate_count"] for feature in features),
        "semantic_classified_count": sum(feature["semantic_classified_count"] for feature in features),
        "avg_interventions": round(total_interventions / len(features), 1) if features else 0,
        "rework": sum(feature["rework_count"] for feature in features),
        "findings": sum(int(feature["review_findings_count"] or 0) for feature in features),
        "revisions": sum(feature["revision_count"] for feature in features),
        "loop_count": sum(feature["loop_count"] for feature in features),
        "loop_total": len(features),
        "pending_entries": sum(feature["pending_entries"] for feature in features),
        "collection_observed_from_start": sum(
            int(feature["collection_coverage"] == "observed_from_start") for feature in features
        ),
        "collection_partial": sum(int(feature["collection_coverage"] == "partial") for feature in features),
        "collection_unknown": sum(int(feature["collection_coverage"] == "unknown") for feature in features),
        "stage_counts": stage_counts,
        "resource_usage": resource_usage,
        "code_delta": _sum_numeric_dicts([feature["code_delta"] for feature in features], CODE_DELTA_KEYS),
        "qualified_lead_time_count": len(qualified_lead_times),
        "avg_qualified_lead_time_sec": (
            round(sum(qualified_lead_times) / len(qualified_lead_times))
            if qualified_lead_times
            else None
        ),
        "invalid_log_count": len(invalid_logs),
        "invalid_logs": invalid_logs,
        "finalization_issue_count": sum(int(not feature["telemetry_finalized"]) for feature in features),
        "resource_missing_entries": resource_usage["missing_entries"],
        "semantic_incomplete_count": semantic_incomplete,
    }
    return {
        "summary": summary,
        "features": features,
        "user_project": user_project_rows,
        "project_view": project_view,
    }


@dataclass
class UsageDashboardData:
    """Request-local facts shared by first-seen tracking, totals and detail views."""

    projects: list[dict]
    aggregate: dict



def load_usage_dashboard_data(
    roots: list[str], rejected_paths: set[str] | None = None,
    linked_supplements: list[LinkedStageSupplement] | None = None,
) -> UsageDashboardData:
    """Read current admitted facts once; never cache telemetry across requests."""
    projects = discover_dashboard_projects(roots, rejected_paths, linked_supplements)
    return UsageDashboardData(projects, aggregate_all(projects))


METRIC_TIPS = {
    "project_total": "当前纳入统计的项目数量。",
    "user_total": "按 eval-log 中的 user_name 去重统计已观测用户数量。",
    "feature_total": "当前纳入统计的项目记录数。",
    "archived": "已完成归档的项目数 / 已发现项目总数。",
    "token_summary": "全部项目已采集 Token 的合计。",
    "duration_summary": "已归档项目的阶段累计耗时之和 ÷ 有效项目数。",
    "flow_state": "流程状态只表示需求工作流事实：已完成、进行中、受阻、执行失败、部分完成或已归档。",
    "verify_pass": "验收通过数 / 有验收结论的需求数。",
    "interventions": "用户有效介入轮次：不含单纯确认，包含方案讨论、需求澄清、方向纠偏和上下文补充。",
    "user_interactions": "用户交互轮次：按阶段记录累计已采集的用户消息轮次，包含确认、讨论、澄清和补充；不等同于有效介入或返工。部分采集显示小计与覆盖数，未采集不计为 0。",
    "rework": "被记录为返工的阶段次数，通常意味着前一阶段输出需要明显调整。",
    "findings": "审查阶段发现的问题总数。",
    "features": "该行覆盖的 feature 数量。",
    "user_features": "该用户参与过的 HelixHarness 需求数量。",
    "users": "该项目中按 user_name 去重的已观测用户数。",
    "evidence": "验收有依据，表示这个需求的验收结论能在 verify-report.md、verify-report.md 或 tasks.md 中找到对应测试命令、执行结果和结论说明。",
    "avg_interventions": "平均每个 feature 需要用户介入多少轮。越低通常表示流程更顺，但需要结合需求复杂度看。",
    "collection": "数据状态只回答异步明细是否已经关联齐全；它不是业务完成状态。",
    "funnel": "按工作流版本应执行的步骤计算。HelixHarness 为 3 步。",
    "quality_facts": "验收结果读取 eval-log 及已关联 Supplement 的 quality 字段：quality.verify_passed 是结论，quality.evidence_complete 是验收依据，review 阶段的 quality.review_findings_count 累计为审查问题；缺值就显示无结论或未采集。",
    "model_interaction": "展示该阶段的命令、阶段、会话、消息规模和模型用量。默认不展示完整 prompt/response 原文。",
    "resource_usage": "Token 数字是已成功采集记录的 resource_usage.total_tokens 合计。5/5 表示 5 条应采集流程记录都有 collection_status=collected 和 total_tokens；它不是通过率、模型数或质量评分。",
    "code_delta": "已归档需求按固定基线、实现提交和评审文件范围补算归档净代码变更，每个需求只统计一次。不同口径不在同一需求内累加；二进制文件计入文件数，不计文本行数。",
    "contamination": "污染风险表示归因边界内可能混入无关问答或无关代码改动的风险。high/medium/low 不是质量分，只是解释 token 和 diff 时的谨慎程度。",
    "focus_tags": "关注标签只来自可计数事实：工作流缺口、采集状态、验收结论、返工、跨阶段修正、审查问题和介入分类。",
    "project_detail": "项目明细是当前用户参与过的项目聚合，一行代表一个项目。",
    "feature_detail": "需求明细是具体 feature/eval-log 的聚合，一行代表一个需求，可继续展开到阶段过程。",
    "duration": "从阶段开始到阶段结束实际经过的时间，不扣除等待；需求总耗时会累计重新执行和跨会话续接产生的实际耗时。",
    "avg_duration": "总览仅按已归档且有耗时记录的需求计算平均阶段累计耗时；用户维度只累计该用户实际参与的阶段，不把整个需求耗时归因给个人。",
    "user_duration": "该用户实际参与阶段的累计耗时 / 参与需求数；不包含同一需求中其他用户负责阶段的耗时。",
    "breakdown": "介入标签按用户消息分类累计：方案讨论、需求澄清、方向纠偏、上下文补充。",
    "loop_usage": "Loop 使用读取账本 trigger=loop 对应的调用标记；未记录的轮次预算保持未知，不推断成功率、实际轮次或终止原因。",
    "revision": "调整事件逐条读取 eval-log 及已关联 Supplement：friction.rework_from_previous=true 计明显返工；否则 workflow.revises_previous_stage=true 或 revised_stage_kind 有值计小幅修正；同一条记录只计一次。",
    "status_stage": "归档事实成立时按流程终态展示 HelixHarness 3/3；缺少阶段记录时只标记数据缺失，不影响业务进度。最近阶段是 eval-log 最后一条流程记录所在阶段。",
    "tool_failures": "表示某次命令、文件读写或其他工具执行未成功；它不等于整个需求失败，后续可能已重试成功。",
}


def _tip(label: str, key: str) -> str:
    text = METRIC_TIPS.get(key, "暂无说明")
    return f'<span class="helixharness-tip-wrap">{_esc(label)}<span class="helixharness-tip" tabindex="0" aria-label="说明">i</span><span class="helixharness-tip-box">{_esc(text)}</span></span>'


def _card(num, label, color=_ACCENT, sub="", tip_key="", raw: bool = False) -> str:
    label_html = _tip(label, tip_key) if tip_key else _esc(label)
    value_html = str(num) if raw else _esc(num)
    value_class = "helixharness-metric-num raw" if raw else "helixharness-metric-num"
    return (f'<div class="helixharness-metric" style="--metric:{color};">'
            f'<div class="helixharness-metric-head"><span>{label_html}</span><i></i></div>'
            f'<div class="{value_class}">{value_html}</div>'
            f'<div class="helixharness-metric-sub">{_esc(sub)}</div></div>')


def _status_badge(status: str) -> str:
    if status == "archived":
        color, text = _GRAY, "已归档"
    else:
        color, text = _GREEN, "进行中"
    return f'<span class="helixharness-badge" style="background:{color}22;color:{color};">{_esc(text)}</span>'


def _entry_status_badge(status: str, *, count: int | None = None) -> str:
    color, text = {
        "in_progress": (_BLUE, "执行中"),
        "completed": (_GREEN, "已完成"),
        "blocked": (_GOLD, "受阻"),
        "failed": (_RED, "执行失败"),
        "partial": (_GOLD, "部分完成"),
        "pending": (_GRAY, "待执行"),
    }.get(status, (_BLUE, status or "未知"))
    if count is not None:
        text = f"{text} {count} 次"
    return f'<span class="helixharness-badge" style="background:{color}22;color:{color};">{_esc(text)}</span>'


def _entry_outcome_text(entry: dict) -> str:
    reason = str(entry.get("outcome_reason") or entry.get("end_reason") or "").strip()
    failure = str(entry.get("failure_code") or "").strip()
    labels = {
        "workflow_completed": "本阶段已完成",
        "workflow_continued": "已转入后续会话继续",
        "workflow_in_progress": "本阶段执行中",
        "workflow_blocked": "本阶段受阻",
        "workflow_partial": "本阶段部分完成",
        "prerequisite_missing": "缺少前置条件",
        "needs_context": "需要补充上下文",
        "user_cancelled": "用户取消",
        "agent_failed": "Agent 执行失败",
        "artifact_validation_failed": "产物校验失败",
        "external_runner_error": "外部执行器异常",
        "git_operation_failed": "Git 操作失败",
        "telemetry_finalize_failed": "统计收口失败",
        "agent_stopped_without_terminal_marker": "Agent 未正常收口",
        "session_ended": "会话已结束",
        "host_rate_limited": "宿主限流",
        "host_overloaded": "宿主过载",
        "host_authentication_failed": "宿主认证失败",
        "host_billing_error": "宿主计费异常",
        "host_model_not_found": "模型不可用",
        "host_server_error": "宿主服务异常",
    }
    status_labels = {
        "in_progress": "本阶段执行中",
        "continued": "已转入后续会话继续",
        "completed": "本阶段已完成",
        "blocked": "本阶段受阻",
        "partial": "本阶段部分完成",
        "failed": "本阶段执行失败",
    }
    status = str(entry.get("status") or "").strip()
    return labels.get(failure) or labels.get(reason) or failure or reason or status_labels.get(status, "未记录")


def _bool_badge(value, true_text="通过", false_text="未通过", none_text="未知") -> str:
    if value is True:
        return f'<span class="helixharness-badge" style="background:{_GREEN}22;color:{_GREEN};">{true_text}</span>'
    if value is False:
        return f'<span class="helixharness-badge" style="background:{_RED}22;color:{_RED};">{false_text}</span>'
    return f'<span class="helixharness-badge" style="background:{_GRAY}22;color:{_MUTED};">{none_text}</span>'


def _conf_badge(value: str) -> str:
    value = value or "unknown"
    color = _GREEN if value == "high" else (_GOLD if value == "medium" else _GRAY)
    label = {"high": "身份可靠", "medium": "身份一般", "low": "身份较低", "unknown": "身份未知"}.get(value, value)
    title = {
        "high": "用户名来源可信",
        "medium": "用户名来源可用，但稳定性一般",
        "low": "用户名来源不稳定，聚合时需要谨慎",
        "unknown": "用户名来源可信度未知",
    }.get(value, f"身份可信度：{value}")
    return f'<span class="helixharness-badge" title="{_esc(title)}" style="background:{color}22;color:{color};">{_esc(label)}</span>'


def _coverage_badge(value: str) -> str:
    color, label = {
        "observed_from_start": (_GREEN, "从需求起点观测"),
        "partial": (_GOLD, "中途开启统计"),
    }.get(value, (_GRAY, "覆盖状态未知"))
    return f'<span class="helixharness-badge" style="background:{color}22;color:{color};">{label}</span>'


def _resource_badge(usage: dict) -> str:
    eligible = int(usage.get("eligible_entries", 0) or 0)
    collected = int(usage.get("collected_entries", 0) or 0)
    if usage.get("exact"):
        color, label = _GREEN, f"Token 数据完整 {collected}/{eligible} 条记录"
    elif collected:
        color, label = _GOLD, f"Token 已采集 {collected}/{eligible} 条记录"
    else:
        color, label = _GRAY, "Token 不可用"
    return f'<span class="helixharness-badge" style="background:{color}22;color:{color};">{_esc(label)}</span>'


def _flow_state(feature: dict) -> tuple[str, str, str]:
    """Return stable workflow state code, label, and color for one requirement."""

    if feature.get("status") == "archived" or feature.get("archive_completed"):
        return "archived", "已归档", _GREEN
    if feature.get("workflow_completed"):
        return "completed", "已完成", _GREEN
    latest_status = str(feature.get("latest_stage_status") or "").lower()
    if latest_status == "blocked":
        return "blocked", "受阻", _RED
    if latest_status == "failed":
        return "failed", "执行失败", _RED
    if latest_status == "partial":
        return "partial", "部分完成", _GOLD
    return "in_progress", "进行中", _BLUE


def _platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(str(platform or "").lower(), platform or "未采集")


def _feature_runtime_summary(feature: dict) -> str:
    versions = [str(version) for version in feature.get("version_set", []) if version and version != "unknown"]
    platforms = [_platform_label(platform) for platform in feature.get("platform_set", []) if platform]
    return (
        f'HelixHarness {" / ".join(versions) if versions else "未采集"} · '
        f'平台 {" / ".join(dict.fromkeys(platforms)) if platforms else "未采集"}'
    )


def _entry_runtime_summary(entry: dict) -> str:
    version = str(entry.get("helixharness_version") or "未采集")
    return f'HelixHarness {version} · {_platform_label(_entry_platform(entry))}'


def _completed_stage_count(feature: dict) -> int:
    if feature.get("status") == "archived" or feature.get("archive_completed"):
        return int(feature.get("expected_stage_count") or 0)
    return sum(1 for stage in feature.get("stage_timeline", []) if stage.get("status") == "completed")


def _recorded_stage_count(feature: dict) -> int:
    return sum(1 for stage in feature.get("stage_timeline", []) if int(stage.get("entry_count", 0) or 0) > 0)


def _record_coverage_text(feature: dict) -> str:
    expected = int(feature.get("expected_stage_count") or 0)
    recorded = _recorded_stage_count(feature)
    if (feature.get("status") == "archived" or feature.get("archive_completed")) and expected and recorded < expected:
        return f"阶段记录 {recorded}/{expected}"
    return ""


def _progress_text(feature: dict) -> str:
    parts = [
        "HelixHarness",
        f'{_completed_stage_count(feature)}/{int(feature.get("expected_stage_count") or 0)} 步骤',
    ]
    bug_fix_count = int(feature.get("bug_fix_count", 0) or 0)
    if bug_fix_count:
        parts.append(f"Bug Fix {bug_fix_count} 次")
    return " · ".join(parts)


def _progress_hint(feature: dict) -> str:
    parts = [f'最近阶段：{_stage_label(feature["current_stage"])}']
    record_coverage = _record_coverage_text(feature)
    if record_coverage:
        parts.append("数据缺失")
    elif _flow_state(feature)[0] != "archived":
        parts.append(_flow_state(feature)[1])
    return " · ".join(parts)


def _progress_hint_html(feature: dict) -> str:
    latest_stage = f'最近阶段：{_stage_label(feature["current_stage"])}'
    record_coverage = _record_coverage_text(feature)
    if record_coverage:
        expected = int(feature.get("expected_stage_count") or 0)
        recorded = _recorded_stage_count(feature)
        missing_title = f"业务已归档，但预期 {expected} 个阶段只采集到 {recorded} 个阶段的记录"
        return (
            f'<span>{_esc(latest_stage)}</span> '
            f'<span class="helixharness-badge" title="{_esc(missing_title)}" '
            f'style="background:{_GOLD}22;color:{_GOLD};">数据缺失</span>'
        )
    if _flow_state(feature)[0] == "archived":
        return f'<span>{_esc(latest_stage)}</span>'
    return f'<span>{_esc(latest_stage)} · {_esc(_flow_state(feature)[1])}</span>'


def _code_attribution_line(delta: dict) -> str:
    source = delta.get("source") or "unknown"
    attribution = delta.get("attribution") or "unknown"
    if source == "worktree_snapshot" and attribution == "exact_stage_window":
        return "统计当前阶段开始到结束之间的工作区变化"
    if source == "worktree_snapshot":
        return "根据阶段执行时的工作区变化统计"
    if source == "mixed" or attribution == "mixed":
        return "变更来自多个采集时段，可能包含其他阶段的修改"
    return "代码变更的统计范围未记录"


def _stage_seg(
    node: dict,
    current_stage: str,
    has_pending_after: bool = False,
) -> str:
    sk = node["stage_kind"]
    label = _stage_label(sk)
    is_current = sk == current_stage and has_pending_after
    if node["status"] != "pending":
        interactions = node.get("user_interactions")
        n = _user_interaction_value(interactions)
        attempts = int(node.get("attempt_count", node.get("entry_count", 1)) or 0)
        continuation_segments = int(node.get("continuation_segment_count", 0) or 0)
        color = {"completed": _GREEN, "failed": _RED, "blocked": _RED, "partial": _GOLD}.get(node["status"], _BLUE)
        duration = _fmt_duration(node.get("duration_sec"))
        continuation_hint = f" · 跨会话续接 {continuation_segments} 段" if continuation_segments else ""
        title = (
            f'{label}：执行 {attempts} 次{continuation_hint} · 用户交互轮次 {n} · {_user_interaction_hint(interactions)} · '
            f'累计耗时 {duration} · Token {node.get("collection_status", "unknown")}'
        )
        return (f'<div class="helixharness-seg done {"current" if is_current else ""}" style="background:{color};" title="{_esc(title)}">'
                f'<div class="helixharness-seg-num">{attempts}<small>次</small></div><div class="helixharness-seg-lbl">{_esc(label)}</div>'
                f'<div class="helixharness-seg-time">交互 {n}{"（小计）" if (interactions or {}).get("collected_entries") and not (interactions or {}).get("exact") else ""} · {_esc(duration)}</div></div>')
    return (f'<div class="helixharness-seg pending" title="{_esc(label)}：未开始">'
            f'<div class="helixharness-seg-num">—</div><div class="helixharness-seg-lbl">{_esc(label)}</div></div>')


def _breakdown_summary(breakdown: dict[str, int]) -> str:
    if not breakdown:
        return "暂无介入标签"
    parts = [f'{BREAKDOWN_LABELS[key]} {int(breakdown.get(key, 0) or 0)}' for key in BREAKDOWN_KEYS]
    return " · ".join(parts)


def _revision_stage_label(stage: str) -> str:
    return _stage_label(stage or "") or "未知阶段"


def _supplement_reason_label(reason: str) -> str:
    return SUPPLEMENT_REASON_LABELS.get(reason or "unknown", reason or "阶段补充")


def _attempt_badges_html(attempt: dict) -> str:
    attempt_index = int(attempt.get("attempt_index", 1) or 1)
    attempt_count = int(attempt.get("attempt_count", 1) or 1)
    segment_index = int(attempt.get("segment_index", 1) or 1)
    segment_count = int(attempt.get("segment_count", 1) or 1)
    parts = [f'<span class="helixharness-attempt-pill">第 {attempt_index}/{attempt_count} 次执行</span>']
    if attempt.get("execution_kind") == "rerun":
        parts.append('<span class="helixharness-attempt-pill rerun">重新执行</span>')
    if segment_count > 1:
        parts.append(f'<span class="helixharness-attempt-pill continuation">跨会话 {segment_index}/{segment_count}</span>')
    if attempt.get("is_latest_entry") and (attempt_count > 1 or segment_count > 1):
        parts.append('<span class="helixharness-attempt-pill latest">最新一次</span>')
    return "".join(parts)


def _revision_summary(row: dict) -> str:
    count = int(row.get("revisions", row.get("revision_count", 0)) or 0)
    pending = int(row.get("pending_entries", 0) or 0)
    parts = []
    if count:
        parts.append(f"跨阶段修正 {count}")
    if pending:
        parts.append(f"采集中 {pending}")
    return " · ".join(parts) if parts else "无跨阶段修正"


def _stage_duration_summary(f: dict) -> str:
    parts = []
    for node in f.get("stage_timeline", []):
        if node.get("status") == "pending":
            continue
        label = _stage_label(node.get("stage_kind", ""))
        parts.append(f'{label} {_fmt_duration(node.get("duration_sec"))}')
    return " · ".join(parts) if parts else "暂无阶段耗时"


def _stage_duration_share_html(
    features: list[dict],
    title: str = "阶段耗时占比",
    actor_name: str | None = None,
) -> str:
    stage_order = STAGE_ORDER
    totals = {stage: 0 for stage in stage_order}
    sample_counts = {stage: 0 for stage in stage_order}
    for feature in features:
        source = feature.get("entries", []) if actor_name else feature.get("stage_timeline", [])
        feature_totals = {stage: 0 for stage in stage_order}
        observed_stages: set[str] = set()
        for node in source:
            if actor_name and _actor(node)["user_name"] != actor_name:
                continue
            stage = node.get("stage_kind")
            if stage not in totals or (not actor_name and node.get("status") == "pending"):
                continue
            duration = _duration_sec(node) if actor_name else _to_int_or_none(node.get("duration_sec"))
            if duration is None:
                continue
            observed_stages.add(stage)
            feature_totals[stage] += max(duration, 0)
        for stage in observed_stages:
            totals[stage] += feature_totals[stage]
            sample_counts[stage] += 1
    total = sum(totals.values())
    if not total:
        return (
            '<div class="helixharness-panel"><div class="helixharness-section-title">'
            f'{_esc(title)}</div><div class="helixharness-empty-mini">暂无可计算的阶段耗时。</div></div>'
        )
    ring_colors = {
        "requirement": "#1677FF",
        "planning": "#36CFC9",
        "coding": "#13A8A8",
        "review": "#722ED1",
        "testing": "#52C41A",
        "archive": "#FAAD14",
    }
    rings = []
    for stage in stage_order:
        seconds = totals[stage]
        sample_count = sample_counts[stage]
        if not sample_count:
            continue
        percent = seconds / total * 100
        label = _stage_label(stage)
        average_seconds = round(seconds / sample_count)
        rings.append(
            '<div class="helixharness-duration-item">'
            f'<div class="helixharness-duration-ring" role="img" aria-label="{_esc(label)}占比 {percent:.1f}%，平均耗时 {_esc(_fmt_duration(average_seconds))}" '
            f'style="--share:{percent:.1f}%;--ring:{ring_colors[stage]};">'
            '<div class="helixharness-duration-ring-core">'
            f'<strong>{percent:.1f}%</strong><span>{_esc(_fmt_duration(average_seconds))}</span>'
            '</div></div>'
            f'<b>{_esc(label)}</b><small>{sample_count} 个需求样本</small>'
            '</div>'
        )
    return (
        '<div class="helixharness-panel helixharness-duration-share"><div class="helixharness-section-title">'
        f'{_esc(title)}</div><p>圆环显示阶段耗时占比；圆内耗时为当前范围内该阶段的平均值。</p>'
        f'<div class="helixharness-duration-ring-grid">{"".join(rings)}</div></div>'
    )


def _grouped_stage_duration_share_html(
    features: list[dict], *, from_entries: bool = False, controls_html: str = "",
) -> str:
    heading = (
        '<div class="helixharness-panel helixharness-duration-share"><div class="helixharness-section-title">阶段耗时占比</div>'
        + controls_html
    )
    groups = [
        ("需求与计划", ("requirement", "planning"), "#1677FF"),
        ("开发与验证", ("coding", "review", "testing"), "#13A8A8"),
        ("归档", ("archive",), "#FAAD14"),
    ]
    totals = {label: 0 for label, _, _ in groups}
    sample_counts = {label: 0 for label, _, _ in groups}
    for feature in features:
        timeline = feature.get("stage_timeline", [])
        if from_entries:
            timeline = [
                {"stage_kind": entry.get("stage_kind"), "status": entry.get("status"), "duration_sec": _duration_sec(entry)}
                for entry in feature.get("entries", [])
            ]
        for label, stages, _ in groups:
            seconds = 0
            observed = False
            for node in timeline:
                if node.get("stage_kind") not in stages or node.get("status") == "pending":
                    continue
                duration = _to_int_or_none(node.get("duration_sec"))
                if duration is None:
                    continue
                observed = True
                seconds += max(duration, 0)
            if observed:
                totals[label] += seconds
                sample_counts[label] += 1
    total = sum(totals.values())
    if not total:
        return (
            heading +
            '<div class="helixharness-empty-mini">暂无可计算的阶段耗时。</div></div>'
        )
    rings = []
    for label, _, color in groups:
        seconds = totals[label]
        sample_count = sample_counts[label]
        percent = seconds / total * 100
        average_seconds = round(seconds / sample_count) if sample_count else None
        sample_text = f"{sample_count} 个项目样本" if sample_count else "暂无项目样本"
        rings.append(
            '<div class="helixharness-duration-item">'
            f'<div class="helixharness-duration-ring" role="img" aria-label="{_esc(label)}占比 {percent:.1f}%，平均耗时 {_esc(_fmt_duration(average_seconds))}" '
            f'style="--share:{percent:.1f}%;--ring:{color};">'
            '<div class="helixharness-duration-ring-core">'
            f'<strong>{percent:.1f}%</strong><span>{_esc(_fmt_duration(average_seconds))}</span>'
            '</div></div>'
            f'<b>{_esc(label)}</b><small>{_esc(sample_text)}</small>'
            '</div>'
        )
    return (
        heading +
        f'<div class="helixharness-duration-ring-grid">{"".join(rings)}</div></div>'
    )


def _requirement_entries(feature: dict) -> list[dict]:
    """Exclude administrative commands from workflow activity statistics."""
    return [entry for entry in feature.get("entries", []) if isinstance(entry, dict)
            and entry.get("command") not in {"init", "status"}
            and (entry.get("command") in COMMANDS or entry.get("stage_kind") in STAGE_ORDER)]


def _latest_runtime_records(features: list[dict]) -> list[tuple[dict, dict]]:
    """Count every project once, classifying it by its latest dated workflow entry.

    Projects without a dated entry stay in the denominator under 未采集.
    """
    records = []
    for feature in features:
        dated = []
        for index, entry in enumerate(_requirement_entries(feature)):
            actual = _parse_time(_latest_log_time([entry.get("ended_at"), entry.get("started_at")]))
            if actual is not None:
                dated.append((actual, str(entry.get("entry_id") or ""), index, entry))
        records.append((feature, max(dated, key=lambda item: item[:3])[3] if dated else {}))
    return records


def _recent_growth_counts(
    features: list[dict], days: int = 7, *, now: datetime | None = None, first_seen: dict | None = None,
) -> dict:
    """Count durable platform admissions within inclusive Beijing calendar days."""
    days = days if days in {1, 3, 7, 30} else 7
    beijing = timezone(timedelta(hours=8))
    today = (now or datetime.now(timezone.utc)).astimezone(beijing).date()
    first_day = today - timedelta(days=days - 1)
    lower = datetime.combine(first_day, datetime.min.time(), beijing)
    upper = datetime.combine(today + timedelta(days=1), datetime.min.time(), beijing)
    snapshot = first_seen or {}
    project_dates = snapshot.get("projects", {})
    user_dates = snapshot.get("users", {})
    projects = missing = 0
    names = set()
    for feature in features:
        admitted = _parse_time(project_dates.get((feature.get("repository", ""), feature.get("feature_identity", feature["feature"]))))
        if admitted is None:
            missing += 1
        else:
            projects += int(lower <= admitted < upper)
        names.update(_actor(entry)["user_name"] for entry in _requirement_entries(feature)
                     if _is_countable_user(_actor(entry)["user_name"]))
    users = sum(lower <= admitted < upper for name in names if (admitted := _parse_time(user_dates.get(name))) is not None)
    return {"projects": projects, "users": users, "days": days, "start": first_day.isoformat(),
            "end": today.isoformat(), "unknown_projects": missing}


def _recent_growth_html(features: list[dict], days: int, first_seen: dict | None = None) -> str:
    counts = _recent_growth_counts(features, days, first_seen=first_seen)
    buttons = "".join(
        f'<button type="submit" name="recent_days" value="{value}" form="helixharness-overview-form" '
        f'aria-current="{str(value == counts["days"]).lower()}">{label}</button>'
        for value, label in ((1, "今日"), (3, "近 3 天"), (7, "近 7 天"), (30, "近 30 天"))
    )
    note = "按平台首次收录时间统计（北京时间），重复同步、更新和归档不重复计数。"
    if counts["unknown_projects"]:
        note += f' {counts["unknown_projects"]} 个历史项目的收录时间未知，未纳入新增统计。'
    return (
        f'<section class="helixharness-recent-growth" data-days="{counts["days"]}">'
        f'<div class="helixharness-recent-heading"><b>{"今日新增" if counts["days"] == 1 else "近期新增"}</b>'
        f'<nav aria-label="近期新增统计范围">{buttons}</nav>'
        f'<span>{counts["start"]} 至 {counts["end"]}</span></div>'
        f'<div class="helixharness-recent-numbers"><span>新增项目 <strong data-new-projects>{counts["projects"]}</strong> 个</span>'
        f'<span>新增用户 <strong data-new-users>{counts["users"]}</strong> 人</span></div>'
        f'<p>{note}</p></section>'
    )


def _latest_user_runtime_records(features: list[dict]) -> list[tuple[dict, dict]]:
    """Use one latest dated requirement entry per named person across all projects."""
    latest: dict[str, tuple[tuple, dict, dict]] = {}
    for feature in features:
        for index, entry in enumerate(feature.get("entries", [])):
            if not isinstance(entry, dict):
                continue
            if entry.get("command") in {"init", "status"}:
                continue
            if entry.get("command") not in COMMANDS and entry.get("stage_kind") not in STAGE_ORDER:
                continue
            name = _actor(entry)["user_name"]
            actual = _parse_time(_latest_log_time([entry.get("ended_at"), entry.get("started_at")]))
            if not _is_countable_user(name) or actual is None:
                continue
            # Stable tie-breaks keep results independent of repository discovery order.
            rank = (actual, str(feature.get("project_id") or feature.get("project_name") or ""),
                    str(feature.get("feature") or ""), str(entry.get("entry_id") or ""), index)
            if name not in latest or rank > latest[name][0]:
                latest[name] = (rank, feature, entry)
    return [(feature, entry) for _, feature, entry in (latest[name] for name in sorted(latest))]


def _runtime_value(entry: dict, dimension: str) -> str:
    value = str(entry.get("helixharness_version") or "").strip() if dimension == "version" else _entry_platform(entry)
    if value.lower() in {"", "unknown", "none", "null", "未采集"}:
        return "未采集"
    return value if dimension == "version" else _platform_label(value)


def _version_sort_key(label: str) -> tuple:
    """Sort numeric versions descending, releases before prereleases, unknown last."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)+)(?:[-–]([^+]+))?(?:\+.*)?", label, re.IGNORECASE)
    if not match:
        return (-1 if label == "未采集" else 0, (), False, (), label)
    core = tuple(int(part) for part in match.group(1).split("."))
    suffix = match.group(2) or ""
    parts = tuple((1, int(part)) if part.isdigit() else (0, part.lower()) for part in re.findall(r"\d+|\D+", suffix))
    return (1, core, not suffix, parts, label)


def _runtime_distribution_html(features: list[dict], dimension: str) -> str:
    """Count users by their latest version and platform."""
    title = {"version": "HelixHarness 版本", "platform": "平台占比"}[dimension]
    unit = "人"
    target = "用户"
    latest_records = _latest_user_runtime_records(features)
    counts: dict[str, int] = {}
    records: dict[str, list[tuple[dict, dict]]] = {}
    for feature, entry in latest_records:
        label = _runtime_value(entry, dimension)
        counts[label] = counts.get(label, 0) + 1
        records.setdefault(label, []).append((feature, entry))
    total = sum(counts.values())
    hint = "每人按最近一条需求记录计数一次；点击查看人员及对应需求。"
    heading = (
        f'<section class="helixharness-panel helixharness-runtime-distribution" data-distribution="{dimension}">'
        f'<div class="helixharness-section-title">{title}</div>'
        f'<p>{hint}</p>'
    )
    if not total:
        return heading + f'<div class="helixharness-empty-mini">暂无可统计的{target}记录。</div></section>'
    colors = ("#1677FF", "#13A8A8", "#FAAD14", "#722ED1", "#EB2F96", "#52C41A", "#FA8C16", "#597EF7")
    segments, items, details = [], [], []
    bars = []
    peak = max(counts.values())
    cumulative = 0
    ordered = (sorted(counts.items(), key=lambda item: _version_sort_key(item[0]), reverse=True)
               if dimension == "version" else sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    for index, (label, count) in enumerate(ordered):
        color = colors[index % len(colors)]
        share = count / total * 100
        start = cumulative / total * 100
        cumulative += count
        detail_id = _html_id(f"helixharness-runtime-{dimension}", label, index)
        segments.append(
            f'<a href="#{detail_id}" aria-label="{_esc(label)} {count} {unit}，{share:.1f}%，查看{target}">'
            f'<title>{_esc(label)} · {count} {unit} · {share:.1f}%</title>'
            f'<circle cx="64" cy="64" r="55" fill="none" stroke="{color}" stroke-width="14" '
            f'pathLength="100" stroke-dasharray="{share:.6f} {100 - share:.6f}" '
            f'stroke-dashoffset="{-start:.6f}" transform="rotate(-90 64 64)" /></a>'
        )
        items.append(
            f'<li data-label="{_esc(label)}" data-count="{count}"><a href="#{detail_id}">'
            f'<i style="background:{color}"></i><span>{_esc(label)}</span>'
            f'<small>{count} {unit}</small><b>{share:.1f}%</b></a></li>'
        )
        if dimension == "version":
            bars.append(
                f'<li data-label="{_esc(label)}" data-count="{count}"><a href="#{detail_id}" '
                f'aria-label="{_esc(label)} {count} {unit}，{share:.1f}%，查看{target}">'
                f'<span class="helixharness-version-label">{_esc(label)}</span>'
                f'<span class="helixharness-version-track" aria-hidden="true"><i style="width:{count / peak * 100:.6f}%"></i></span>'
                f'<span class="helixharness-version-value"><b>{count} {unit}</b><small>{share:.1f}%</small></span></a></li>'
            )
        details.append(_runtime_distribution_detail_html(detail_id, title, label, records[label]))
    if dimension == "version":
        return heading + f'<ul class="helixharness-version-bars" aria-label="{title}，共 {total} {unit}">{"".join(bars)}</ul>{"".join(details)}</section>'
    return (
        heading + '<div class="helixharness-runtime-chart">'
        f'<div class="helixharness-duration-ring helixharness-runtime-ring" role="group" aria-label="{title}，共 {total} {unit}">'
        f'<svg viewBox="0 0 128 128">{"".join(segments)}</svg>'
        f'<div class="helixharness-duration-ring-core"><strong>{total}</strong><span>人</span></div></div>'
        f'<ul class="helixharness-runtime-legend">{"".join(items)}</ul></div>{"".join(details)}'
        '</section>'
    )


def _filter_runtime_features(
    features: list[dict], period: str, start: str = "", end: str = "", *, now: datetime | None = None,
) -> tuple[list[dict], str]:
    """Filter each entry by its recorded event time, using inclusive Beijing dates."""
    if period == "all":
        return features, "全部日志（含未采集时间的记录）"
    beijing = timezone(timedelta(hours=8))
    today = (now or datetime.now(timezone.utc)).astimezone(beijing).date()
    if period in {"7d", "30d"}:
        end_date = today
        start_date = today - timedelta(days=6 if period == "7d" else 29)
    elif period == "custom":
        try:
            start_date = datetime.strptime(start, "%Y-%m-%d").date()
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("请选择有效的开始和结束日期。") from exc
        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期。")
    else:
        raise ValueError("不支持的时间范围。")
    lower = datetime.combine(start_date, datetime.min.time(), beijing)
    upper = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), beijing)
    selected, undated = [], 0
    for feature in features:
        entries = []
        for entry in feature.get("entries", []):
            actual = _parse_time(_latest_log_time([entry.get("ended_at"), entry.get("started_at")]))
            if actual is None:
                undated += 1
            elif lower <= actual < upper:
                entries.append(entry)
        if entries:
            selected.append({**feature, "entries": entries})
    hint = f"{start_date:%Y-%m-%d} 至 {end_date:%Y-%m-%d}（北京时间）"
    if undated:
        hint += f"；{undated} 条无实际时间的日志未纳入"
    return selected, hint


def _runtime_overview_html(
    features: list[dict], period: str = "all", start: str = "", end: str = "", *, expanded: bool = False, view: str = "project", recent_days: int = 1,
) -> str:
    links = []
    for value, label in (("7d", "近 7 天"), ("30d", "近 30 天"), ("all", "全部")):
        current = ' aria-current="true"' if value == period else ""
        links.append(
            f'<a href="/?runtime_period={value}&view={_esc(view)}&recent_days={recent_days}" hx-get="/partial/runtime?runtime_period={value}&view={_esc(view)}&recent_days={recent_days}" '
            f'hx-target="#helixharness-runtime-overview" hx-swap="outerHTML"{current}>{label}</a>'
        )
    try:
        selected, hint = _filter_runtime_features(features, period, start, end)
        message = f'<span class="helixharness-runtime-range-hint">{_esc(hint)}</span>'
    except (ValueError, OverflowError) as exc:
        selected = []
        message = f'<span class="helixharness-runtime-range-error" role="alert">{_esc(str(exc))}</span>'
    controls = (
        '<div class="helixharness-runtime-filter"><span>统计时间</span>'
        f'<nav aria-label="阶段耗时统计时间">{"".join(links)}</nav>'
        '<form action="/" method="get" hx-get="/partial/runtime" hx-target="#helixharness-runtime-overview" '
        'hx-swap="outerHTML" hx-sync="#helixharness-runtime-overview:replace">'
        '<input type="hidden" name="runtime_period" value="custom">'
        f'<input type="hidden" name="view" value="{_esc(view)}">'
        f'<input type="hidden" name="recent_days" value="{recent_days}">'
        f'<input type="date" name="runtime_start" aria-label="开始日期" value="{_esc(start)}" required>'
        '<span>至</span>'
        f'<input type="date" name="runtime_end" aria-label="结束日期" value="{_esc(end)}" required>'
        '<button type="submit">按日期筛选</button></form>'
        f'{message}</div>'
    )
    return (
        f'<div class="helixharness-overview-distributions" id="helixharness-runtime-overview" data-period="{_esc(period)}" data-start="{_esc(start)}" data-end="{_esc(end)}" hx-sync="#helixharness-runtime-overview:replace" '
        'hx-vals="js:{runtime_expanded:document.querySelector(\'.helixharness-runtime-disclosure\').open}">'
        f'{_grouped_stage_duration_share_html(selected, from_entries=True, controls_html=controls)}'
        f'<details class="helixharness-runtime-disclosure"{" open" if expanded else ""}>'
        '<summary><b>版本与平台使用情况</b><span class="helixharness-runtime-toggle" '
        'data-open="查看版本与平台分布" data-close="收起版本与平台分布"></span></summary>'
        '<div class="helixharness-runtime-disclosure-body">'
        '<div class="helixharness-runtime-panels">'
        f'{_runtime_distribution_html(features, "version")}'
        f'{_runtime_distribution_html(features, "platform")}</div>'
        '</div></details></div>'
    )


def render_runtime_overview_html(
    roots: list[str], rejected_paths: set[str] | None = None,
    linked_supplements: list[LinkedStageSupplement] | None = None,
    *, period: str = "all", start: str = "", end: str = "", expanded: bool = True, view: str = "project", recent_days: int = 1,
) -> str:
    """Render the date controls and charts for a dashboard partial refresh."""
    features = aggregate_all(discover_dashboard_projects(roots, rejected_paths, linked_supplements))["features"]
    return _runtime_overview_html(features, period, start, end, expanded=expanded, view=view, recent_days=recent_days)


def _runtime_distribution_detail_html(
    detail_id: str, title: str, label: str, records: list[tuple[dict, dict]],
) -> str:
    """Present runtime distribution members using the distribution's counting scope."""
    unit = "人"
    target = "用户"
    hint = "每名用户按最近一条需求记录统计。"
    rows = []
    for feature, entry in sorted(records, key=lambda record: (str(record[0].get("project_name") or ""), str(record[0].get("feature") or ""))):
        name = _actor(entry)["user_name"]
        name = name if _is_countable_user(name) else "未采集"
        latest = _latest_log_time([entry.get("ended_at"), entry.get("started_at")])
        rows.append(
            f'<tr><td class="helixharness-runtime-member"><b>{_esc(name)}</b></td>'
            f'<td><div class="helixharness-runtime-repository">{_esc(feature.get("project_name") or "未采集")}</div>'
            f'<div class="helixharness-runtime-requirement">{_esc(feature.get("feature") or "未采集")}</div></td>'
            f'<td><span class="helixharness-runtime-version-tag">{_esc(_runtime_value(entry, "version"))}</span></td>'
            f'<td><span class="helixharness-runtime-platform-label">{_esc(_runtime_value(entry, "platform"))}</span></td>'
            f'<td class="helixharness-log-time-cell">{_log_time_html(latest)}</td></tr>'
        )
    return (
        f'<section id="{detail_id}" class="helixharness-detail-page helixharness-runtime-detail" role="dialog" '
        f'aria-modal="true" aria-labelledby="{detail_id}-title" tabindex="-1">'
        f'<div class="helixharness-detail-page-head"><div><b id="{detail_id}-title">{_esc(label)} · {target}明细</b>'
        f'<span>{_esc(title)} · {len(records)} {unit}</span></div>'
        '<a href="#helixharness-runtime-overview">返回占比总览</a></div>'
        '<div class="helixharness-runtime-detail-body"><div class="helixharness-runtime-detail-card">'
        f'<p class="helixharness-runtime-detail-note">{hint}</p>'
        '<div class="helixharness-runtime-detail-scroll" role="region" aria-label="占比明细表格" tabindex="0">'
        '<table class="helixharness-runtime-detail-table"><colgroup><col style="width:17%"><col style="width:35%">'
        '<col style="width:14%"><col style="width:16%"><col style="width:18%"></colgroup><thead><tr>'
        '<th scope="col">最近记录用户</th><th scope="col">对应代码库 / 需求</th><th scope="col">最近使用版本</th>'
        '<th scope="col">最近使用平台</th><th scope="col">最近使用时间（北京时间）</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></div></div></section>'
    )


def _code_delta_label(delta: dict) -> str:
    if delta.get("scope") == "archive_review":
        return "评审范围净代码变更"
    return "归档净代码变更" if delta.get("scope") == "feature_archive" else "代码行数变更"


def _archive_documents_html(delta: dict) -> str:
    """Render document edits separately from explicit active/completed moves."""
    if delta.get("collection_status") != "collected":
        reason = ARCHIVE_REASONS.get(delta.get("unavailable_reason"), "尚未采集归档文档变更")
        return f'<div class="helixharness-archive-files-unavailable">归档文档更新：{_esc(reason)}</div>'
    changes = delta.get("file_changes", [])
    labels = {"A": "新增", "M": "更新", "D": "删除", "T": "类型变更", "R": "迁移", "RM": "迁移并更新"}
    rows = []
    for item in sorted(changes, key=lambda item: item["path"]):
        before = f'<code>{_esc(item["previous_path"])}</code><br>→ ' if item.get("previous_path") else ""
        kind = ("需求文件迁移" if item.get("previous_path") else
                "知识库更新" if item["path"].startswith("docs/knowledge/") else "归档文档更新")
        rows.append(
            f'<tr><td>{labels.get(item["status"], "更新")}</td>'
            f'<td>{"<b>" + _esc(item["title"]) + "</b><br>" if item.get("title") else ""}{before}<code>{_esc(item["path"])}</code><br><small>{kind}</small></td></tr>'
        )
    return (
        '<details class="helixharness-archive-documents"><summary><span>'
        f'<b>更新内容</b><small>{len(changes)} 个文件</small></span>'
        '<span class="helixharness-stage-attempts-toggle" data-open="查看内容" data-close="收起内容"></span></summary>'
        '<div class="helixharness-archive-files-scroll"><table class="helixharness-archive-files-table">'
        '<thead><tr><th>变更</th><th>文件路径</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></details>'
    )


def _archive_knowledge_html(delta: dict | None) -> str:
    """Show knowledge documents included in archive, excluding code and moved workflow receipts."""
    if not delta:
        return '<div class="helixharness-archive-files-unavailable">知识库更新：未采集知识库变更记录。</div>'
    if isinstance(delta.get("archive_documents"), dict):
        return _archive_documents_html(delta["archive_documents"])
    if delta.get("collection_status") != "collected":
        reason = ARCHIVE_REASONS.get(delta.get("unavailable_reason"), "知识库变更未采集")
        return f'<div class="helixharness-archive-files-unavailable">知识库更新：{_esc(reason)}</div>'
    changes = delta.get("file_changes")
    if not isinstance(changes, list):
        return '<div class="helixharness-archive-files-unavailable">知识库更新：未采集知识库变更记录。</div>'
    knowledge = [item for item in changes if item["path"].startswith("docs/knowledge/")]
    documents = [item for item in knowledge if item["path"].endswith(".md")]
    auxiliary = len(knowledge) - len(documents)
    labels = {"A": "新增", "M": "更新", "T": "类型变更", "D": "删除"}
    order = {"A": 0, "M": 1, "T": 1, "D": 2}
    counts = {key: sum(item["status"] == key for item in documents) for key in labels}
    rows = []
    for item in sorted(documents, key=lambda item: (order[item["status"]], item["path"])):
        status = item["status"]
        path = item["path"]
        title = item.get("title") or path.rsplit("/", 1)[-1]
        kind = "知识索引" if path.rsplit("/", 1)[-1].lower() == "index.md" else "知识文档"
        rows.append(
            f'<tr><td><span class="helixharness-file-status helixharness-file-status-{status.lower()}">{labels[status]}</span></td>'
            f'<td><b>{_esc(title)}</b><br><code>{_esc(path)}</code></td><td>{kind}</td></tr>'
        )
    summary = f'新增 {counts["A"]} · 更新 {counts["M"] + counts["T"]} · 删除 {counts["D"]}'
    hint = '本次归档范围内包含的知识库更新。'
    if auxiliary:
        hint += f'另有 {auxiliary} 项知识库辅助记录变更，不计入知识文档篇数。'
    body = (
        '<div class="helixharness-archive-files-scroll"><table class="helixharness-archive-files-table"><thead><tr>'
        '<th>变更</th><th>知识文档 / 路径</th><th>类别</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    ) if rows else '<p class="helixharness-section-hint">可解析的归档范围内未发现知识文档变更。</p>'
    return (
        '<details class="helixharness-archive-knowledge"><summary>'
        f'<span><b>更新内容 · 知识库更新 {len(documents)} 篇</b><small>{summary}</small></span>'
        '<span class="helixharness-stage-attempts-toggle" data-open="展开知识" data-close="收起知识"></span></summary>'
        f'<p class="helixharness-section-hint">{hint}</p>{body}</details>'
    )


def _implementation_files_html(delta: dict | None) -> str:
    """Show the feature's final implementation files without attributing them to any individual Apply."""
    if not delta or delta.get("collection_status") != "collected" or not isinstance(delta.get("file_changes"), list):
        reason = ARCHIVE_REASONS.get((delta or {}).get("unavailable_reason"), "未采集可解析的归档文件清单")
        return (
            '<section class="helixharness-archive-files-unavailable"><b>实现文件</b>'
            f'<p>{_esc(reason)}</p></section>'
        )
    changes = [
        item for item in delta["file_changes"]
        if "docs" not in item["path"].lower().split("/")
        and not item["path"].lower().endswith((".md", ".rst", ".adoc"))
    ]
    labels = {"A": "新增", "M": "修改", "T": "类型变更", "D": "删除"}
    order = {"A": 0, "M": 1, "T": 1, "D": 2}
    counts = {status: sum(item["status"] == status for item in changes) for status in labels}
    summary = f'新增 {counts["A"]} · 修改 {counts["M"] + counts["T"]} · 删除 {counts["D"]}'
    rows = "".join(
        f'<tr><td><span class="helixharness-file-status helixharness-file-status-{item["status"].lower()}">{labels[item["status"]]}</span></td>'
        f'<td><code>{_esc(item["path"])}</code></td></tr>'
        for item in sorted(changes, key=lambda item: (order[item["status"]], item["path"]))
    )
    body = (
        '<div class="helixharness-archive-files-scroll"><table class="helixharness-archive-files-table"><thead><tr>'
        '<th>变更</th><th>文件路径</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    ) if rows else '<p class="helixharness-section-hint">可解析的归档范围内未发现实现文件变更。</p>'
    review_range = delta.get("scope") == "archive_review"
    heading = "实现文件"
    period = "截至本次评审" if review_range else "截至归档"
    return (
        '<details class="helixharness-implementation-files" ontoggle="if(this.open){const s=window.getSelection();if(s&amp;&amp;s.rangeCount&amp;&amp;s.containsNode(this,true))s.removeAllRanges();}"><summary><span>'
        f'<b>{heading}</b><small>{period} · {len(changes)} 个文件 · {summary}</small></span>'
        '<span class="helixharness-stage-attempts-toggle" data-open="查看文件" data-close="收起文件"></span></summary>'
        f'{body}</details>'
    )


def _artifact_scope_note(paths: list[str], stage: str) -> str:
    if stage == "coding" and all(path.startswith("docs/specs/") and path.endswith(".md") for path in paths):
        return '<small>当前上报仅记录流程文档，未采集本次代码文件清单。</small>'
    return ""


def _stage_total_html(label: str, values: list[int | None], unit: str = "次", *, duration: bool = False) -> str:
    """Show a recorded subtotal with coverage instead of filling missing facts with zero."""
    known = [value for value in values if value is not None]
    total = (_fmt_duration(sum(known)) if duration else f"{sum(known)} {unit}") if known else "未采集"
    coverage = f"（已记录 {len(known)}/{len(values)} 条）" if len(known) != len(values) else ""
    return f'<span>{label} <b>{_esc(total)}</b>{coverage}</span>'


def _stage_execution_summary_html(
    entries: list[dict], detail_cards: str, index: int, archive_changes: dict | None = None,
    *, implementation_html: str = "",
) -> str:
    """Summarize repeated stage runs while retaining every original segment in a disclosure."""
    attempts = _execution_attempt_metadata(entries)
    runs: dict[int, dict] = {}
    for position, entry in enumerate(entries):
        metadata = attempts[str(entry.get("entry_id") or f"entry-{position}")]
        runs[metadata["attempt_index"]] = entry
    latest = entries[-1]
    label = _stage_label(latest.get("stage_kind", ""))
    available = ["async_evidence" not in entry or entry.get("telemetry_status") == "complete" for entry in entries]
    usages = [_resource_usage(entry) if ready else _resource_usage({}) for entry, ready in zip(entries, available)]
    usage = _aggregate_resource_usages(usages, "stage")
    token_label = "Token 累计" if usage["exact"] else "Token 已采集小计"
    artifacts = sorted({str(path) for entry in entries for path in _artifacts(entry)["produced_files"]})
    artifact_html = (
        f'<b>{len(artifacts)} 个文件</b><ul class="helixharness-artifact-list">'
        + "".join(f'<li><code>{_esc(path)}</code></li>' for path in artifacts) + '</ul>'
    ) if artifacts else '<span>未记录产物</span>'
    artifact_html += _artifact_scope_note(artifacts, str(latest.get("stage_kind") or ""))
    artifact_block = (
        f'<section class="helixharness-stage-block"><h4>更新内容</h4><div>{artifact_html}</div></section>'
        if latest.get("stage_kind") != "archive" and latest.get("command") != "archive" else ""
    )
    status_counts: dict[str, int] = {}
    for run in runs.values():
        status = str(run.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    status_html = " · ".join(
        _entry_status_badge(status, count=count)
        + ('<small>（最近一次）</small>' if len(status_counts) > 1 and status == latest.get("status") else "")
        for status, count in status_counts.items()
    )
    duration_html = _stage_total_html("累计耗时", [_duration_sec(entry) for entry in entries], duration=True)
    execution_lines = []
    for field, title in (("tool_call_count", "工具调用"), ("tool_error_count", "调用未成功"), ("subagent_spawn_count", "子 Agent 创建")):
        values = []
        for entry, ready in zip(entries, available):
            raw = entry.get("execution_stats") if isinstance(entry.get("execution_stats"), dict) else {}
            value = _to_int_or_none(raw.get(field)) if ready else None
            if field == "tool_error_count" and ready:
                evidence = entry.get("async_evidence") or {}
                failures = [item for item in evidence.get("failed_tools", []) if isinstance(item, dict)]
                if failures:
                    value = len(failures)
            values.append(value)
        execution_lines.append(_stage_total_html(title, values))
    user_messages = [_entry_user_turns(entry) for entry in entries]
    assistant_messages = []
    for entry, ready in zip(entries, available):
        evidence = entry.get("async_evidence") or {}
        interaction = evidence.get("interaction_summary") or {}
        assistant_messages.append(_to_int_or_none(interaction.get("assistant_messages")) if ready else None)
    interaction_html = "<br>".join((
        _stage_total_html("用户交互轮次", user_messages, "轮"),
        _stage_total_html("AI 回复", assistant_messages, "条"),
    ))
    people = "、".join(dict.fromkeys(_actor(entry)["user_name"] for entry in entries))
    segments = f" · 共 {len(entries)} 段记录" if len(entries) > len(runs) else ""
    return (
        '<article class="helixharness-stage-card helixharness-stage-rollup">'
        f'<div class="helixharness-stage-card-head"><div><span class="helixharness-stage-index">{index}</span><b>{_esc(label)}</b>'
        f'<span class="helixharness-attempt-pill">执行 {len(runs)} 次{segments}</span><span class="helixharness-attempt-pill latest">汇总</span></div>'
        f'<div class="helixharness-stage-head-meta">{_resource_badge(usage)}</div></div>'
        f'<div class="helixharness-stage-meta"><span>/helix:{_esc(latest.get("command", ""))}</span><span>用户 {_esc(people)}</span></div>'
        '<div class="helixharness-stage-summary-grid">'
        '<section class="helixharness-stage-block"><h4>执行汇总</h4><div>'
        f'{status_html}<br>{duration_html}'
        '</div></section>'
        f'{artifact_block}'
        f'<section class="helixharness-stage-block"><h4>{token_label}</h4><div><b>{_fmt_tokens(usage.get("total_tokens"))}</b>'
        f'<br><span>输入 {_fmt_tokens(usage.get("input_tokens"))} · 输出 {_fmt_tokens(usage.get("output_tokens"))}</span></div></section>'
        f'<section class="helixharness-stage-block"><h4>执行情况</h4><div>{"<br>".join(execution_lines)}</div></section>'
        f'<section class="helixharness-stage-block"><h4>对话记录</h4><div>{interaction_html}</div></section></div>'
        f'{_archive_knowledge_html(archive_changes) if latest.get("stage_kind") == "archive" or latest.get("command") == "archive" else ""}'
        f'{implementation_html}'
        '<details class="helixharness-stage-attempts"><summary>'
        f'<span>每次执行详情 · {len(runs)} 次{segments}</span>'
        '<span class="helixharness-stage-attempts-toggle" data-open="展开" data-close="收起"></span></summary>'
        f'<div class="helixharness-stage-list">{detail_cards}</div></details></article>'
    )


def render_feature_timeline_html(eval_log: dict, *, archive_changes: dict | None = None) -> str:
    f = aggregate_feature(eval_log)
    _has_pending = any(n["status"] == "pending" for n in f["stage_timeline"])
    segs = "".join(
        _stage_seg(n, f["current_stage"], _has_pending)
        for n in f["stage_timeline"]
    )
    attempt_metadata = _execution_attempt_metadata(f["entries"])
    archive_changes = archive_changes if archive_changes is not None else f.get("archive_changes")
    archive_indices = [i for i, entry in enumerate(f["entries"], 1) if entry.get("command") == "archive" or entry.get("stage_kind") == "archive"]
    archive_index = archive_indices[-1] if archive_indices else None
    apply_indices = [i for i, entry in enumerate(f["entries"], 1) if entry.get("command") == "apply"]
    apply_index = apply_indices[-1] if apply_indices else None
    group_sizes: dict[tuple[str, str], int] = {}
    for entry in f["entries"]:
        key = (str(entry.get("stage_kind") or ""), _stage_profile_key(entry))
        group_sizes[key] = group_sizes.get(key, 0) + 1
    card_groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    for idx, e in enumerate(f["entries"], start=1):
        key = (str(e.get("stage_kind") or ""), _stage_profile_key(e))
        actor = _actor(e)
        execution = _execution_stats(e)
        artifacts = _artifacts(e)
        usage = _resource_usage(e)
        delta = _code_delta(e)
        workflow = _workflow(e)
        attempt = attempt_metadata.get(str(e.get("entry_id") or ""), {})
        produced = artifacts["produced_files"]
        produced_hint = f'{len(produced)} 个文件' if produced else "—"
        is_archive = e.get("stage_kind") == "archive" or e.get("command") == "archive"
        produced_suffix = "" if is_archive else f" · 已记录产物 {produced_hint}"
        is_async = "async_evidence" in e
        linked = is_async and e.get("telemetry_status") == "complete"
        facts_available = not is_async or linked
        effective_at = _format_beijing_time(
            e.get("association_effective_at"), seconds=True,
        )
        async_evidence = e.get("async_evidence") if isinstance(e.get("async_evidence"), dict) else {}
        interaction_summary = (
            async_evidence.get("interaction_summary")
            if isinstance(async_evidence.get("interaction_summary"), dict)
            else {}
        )
        assistant_messages = _to_int_or_none(interaction_summary.get("assistant_messages"))
        trace = async_evidence.get("workflow_trace") if isinstance(async_evidence.get("workflow_trace"), dict) else {}
        failed_tools = [item for item in async_evidence.get("failed_tools", []) if isinstance(item, dict)]
        failed_gates = [
            item for item in trace.get("gates", [])
            if isinstance(item, dict) and item.get("status") in {"failed", "blocked"}
        ]
        fallbacks = [item for item in trace.get("fallbacks", []) if item]
        resource_bits = [
            f'<b>{_fmt_tokens(usage.get("total_tokens"))}</b>',
            f'<span>输入 {_fmt_tokens(usage.get("input_tokens"))} · 输出 {_fmt_tokens(usage.get("output_tokens"))}</span>',
        ]
        tool_failure_count = len(failed_tools) or execution["tool_error_count"]
        execution_bits = [
            _stage_total_html("工具调用", [execution["tool_call_count"]]),
            _stage_total_html("调用未成功", [tool_failure_count]),
            _stage_total_html("子 Agent 创建", [execution["subagent_spawn_count"]]),
        ]
        interaction_bits = [
            _stage_total_html("用户交互轮次", [_entry_user_turns(e)], "轮"),
            _stage_total_html("AI 回复", [assistant_messages], "条"),
        ]
        process_bits = [
            f'<b>/helix:{_esc(e.get("command", ""))}</b> · {_entry_status_badge(str(e.get("status") or ""))}',
            f'<span>结果：{_esc(_entry_outcome_text(e))}</span>',
            f'<span>第 {int(e.get("attempt", attempt.get("attempt_index", 1)) or 1)} 次执行{produced_suffix}</span>',
        ]
        if workflow.get("supplements_previous_stage"):
            process_bits.append(
                f'<span>补充 {_revision_stage_label(workflow.get("supplemented_stage_kind", ""))} · '
                f'{_supplement_reason_label(workflow.get("supplement_reason", ""))}</span>'
            )
        if workflow.get("stage_repeated") or int(attempt.get("attempt_count", 1) or 1) > 1:
            process_bits.append(f'<span>该阶段共执行 {int(attempt.get("attempt_count", 1) or 1)} 次</span>')
        trace_bits = []
        if failed_gates:
            trace_bits.append(f'<b>失败/阻塞 Gate {len(failed_gates)}</b>')
            trace_bits.append(
                '<span>' + " · ".join(
                    _esc(item.get("name") or item.get("gate") or item.get("id") or "未命名 Gate")
                    for item in failed_gates[:4]
                ) + '</span>'
            )
        if fallbacks:
            trace_bits.append(f'<span>fallback {len(fallbacks)} 次</span>')
        if failed_tools:
            trace_bits.append(f'<b>工具调用未成功 {len(failed_tools)} 次</b>')
            trace_bits.append(
                '<span>涉及工具：' + "、".join(dict.fromkeys(_esc(item.get("name") or "未命名工具") for item in failed_tools[:4])) + '</span>'
            )
        if trace_bits:
            trace_bits.append(
                '<span class="helixharness-stage-exception-note">这里只记录执行过程中的异常调用、阻塞 Gate 或降级路径；'
                '阶段是否完成以“执行结果”为准。</span>'
            )
        artifact_items = "".join(f'<li><code>{_esc(str(path))}</code></li>' for path in produced)
        artifact_block = (
            '<section class="helixharness-stage-block"><h4>更新内容</h4>'
            f'<ul class="helixharness-artifact-list">{artifact_items}</ul>'
            f'{_artifact_scope_note(produced, str(e.get("stage_kind") or ""))}</section>'
            if artifact_items and not is_archive
            else ""
        )
        if not artifact_items and not is_archive:
            note = _artifact_scope_note(produced, str(e.get("stage_kind") or ""))
            artifact_block = f'<section class="helixharness-stage-block"><h4>更新内容</h4>{note}</section>' if note else ""
        has_code_delta = any(_to_int_or_none(delta.get(key)) is not None for key in ("files_changed", "lines_added", "lines_deleted"))
        code_block = ""
        if has_code_delta:
            code_block = (
                '<section class="helixharness-stage-block"><h4>代码变更</h4><div>'
                f'<b>{_fmt_code_delta(delta)}</b><br>{_code_summary_html(delta)}'
                f'<br><small>{_esc(_code_attribution_line(delta))}</small></div></section>'
            )
        has_execution_history = int(attempt.get("attempt_count", 1) or 1) > 1 or int(
            attempt.get("segment_count", 1) or 1
        ) > 1
        if not has_execution_history:
            card_class = "helixharness-stage-card"
        elif attempt.get("is_latest_entry"):
            card_class = "helixharness-stage-card latest-attempt"
        else:
            card_class = "helixharness-stage-card prior-attempt"
        if facts_available:
            async_blocks = (
                f'<section class="helixharness-stage-block"><h4>Token</h4><div>{"<br>".join(resource_bits)}</div></section>'
                f'<section class="helixharness-stage-block"><h4>执行情况</h4><div>{"<br>".join(execution_bits)}</div></section>'
                f'<section class="helixharness-stage-block"><h4>对话记录</h4><div>{"<br>".join(interaction_bits)}</div></section>'
            )
            received_at = _format_beijing_time(
                e.get("supplement_received_at"), seconds=True,
            )
            effective_block = (
                '<section class="helixharness-stage-effective"><span>异步数据关联（北京时间）</span><div>'
                f'<span>report 到达 <b>{_esc(received_at or "未记录")}</b></span>'
                f'<span>统计生效 <b>{_esc(effective_at or "未记录")}</b>（eval-log 发现）</span>'
                '</div></section>'
            ) if linked else ""
        else:
            async_blocks = (
                '<section class="helixharness-stage-block helixharness-stage-block-wide helixharness-async-pending"><h4>异步事实待关联</h4>'
                '<div><b>当前不按 0 统计</b><br><span>report 可先到达；只有发现并关联 eval-log 后，Token、执行和会话采集才从发现时间开始生效。</span></div></section>'
            )
            effective_block = ""
        exception_block = (
            f'<details class="helixharness-stage-block helixharness-stage-exception"><summary>异常详情</summary><div>{"<br>".join(trace_bits)}</div></details>'
            if trace_bits
            else ""
        )
        summary_blocks = "".join((
            f'<section class="helixharness-stage-block"><h4>执行结果</h4><div>{"<br>".join(process_bits)}</div></section>',
            artifact_block,
            async_blocks,
            code_block,
        ))
        card = "".join((
            f'<article class="{card_class}">'
            f'<div class="helixharness-stage-card-head"><div><span class="helixharness-stage-index">{idx}</span>'
            f'<b>{_esc(_stage_label(e.get("stage_kind", "")))}</b>'
            f'{_attempt_badges_html(attempt)}'
            '</div>'
            f'<div class="helixharness-stage-head-meta">{_resource_badge(_aggregate_resource_usages([usage], "entry"))}<b>{_esc(_fmt_duration(_duration_sec(e)))}</b></div></div>'
            f'<div class="helixharness-stage-meta"><span>{_format_beijing_time(e.get("started_at"))} → {_format_beijing_time(e.get("ended_at"))}（北京时间）</span>'
            f'<span>用户 {_esc(actor["user_name"])}</span>'
            f'<span>{_esc(_entry_runtime_summary(e))}</span>'
            f'<span>{"异步事实已关联" if linked else "异步事实待关联"}</span></div>'
            '<div class="helixharness-stage-grid">'
            f'<div class="helixharness-stage-summary-grid">{summary_blocks}</div>',
            exception_block,
            _archive_knowledge_html(archive_changes) if idx == archive_index else "",
            _implementation_files_html(archive_changes) if idx == apply_index and group_sizes[key] == 1 else "",
            effective_block,
            '</div>'
            '</article>',
        ))
        card_groups.setdefault(key, []).append((e, card))
    cards = ""
    for index, group in enumerate(card_groups.values(), start=1):
        detail_cards = "".join(card for _, card in group)
        cards += (
            _stage_execution_summary_html(
                [entry for entry, _ in group], detail_cards, index,
                archive_changes if archive_index is not None and group[-1][0] is f["entries"][archive_index - 1] else None,
                implementation_html=(
                    _implementation_files_html(archive_changes)
                    if apply_index is not None and any(entry is f["entries"][apply_index - 1] for entry, _ in group) else ""
                ),
            )
            if len(group) > 1 else detail_cards
        )
    return (
        f'<div class="helixharness-bar" style="--helixharness-stage-count:{len(f["stage_timeline"])}">{segs}</div>'
        f'<div class="helixharness-stage-list">{cards}</div>'
    )


def _verify_text(row: dict) -> str:
    return f'{row["verify_passed"]}/{row["verify_total"]}' if row.get("verify_total") else "—"


def _evidence_text(row: dict) -> str:
    return f'{row["evidence_complete"]}/{row["evidence_total"]}' if row.get("evidence_total") else "—"


def _attention_tag_items(row: dict) -> list[tuple[str, str, str]]:
    tags: list[tuple[str, str, str]] = []
    feature_count = max(int(row.get("feature_count", 0) or 0), 1)
    completed = int(row.get("workflow_completed", row.get("archived_count", 0)) or 0)
    completion_total = int(row.get("workflow_completion_total", feature_count) or 0)
    if completed < completion_total:
        tags.append(("工作流未完整", _GOLD, f"工作流完整 {completed}/{completion_total}"))
    if int(row.get("pending_entries", 0) or 0):
        tags.append((f'采集中 {row.get("pending_entries", 0)}', _BLUE, "存在异步采集未完成的记录，统计可能稍后更新"))
    if int(row.get("verify_total", 0) or 0) == 0:
        tags.append(("缺少验收", _GOLD, "没有采集到明确的验收结论"))
    elif int(row.get("verify_passed", 0) or 0) < int(row.get("verify_total", 0) or 0):
        tags.append(("验收未全过", _RED, f'验收通过 {row.get("verify_passed", 0)}/{row.get("verify_total", 0)}'))
    if int(row.get("rework", 0) or 0):
        tags.append((f'返工 {row.get("rework", 0)}', _RED, "发生过前序产物需要明显调整、重做或修复的阶段"))
    if int(row.get("revisions", 0) or 0):
        tags.append((f'修正 {row.get("revisions", 0)}', _PURPLE, "发生过跨阶段修正，当前阶段调整了上一更新内容"))
    if int(row.get("findings", 0) or 0):
        tags.append((f'问题 {row.get("findings", 0)}', _RED, "审查阶段记录到的问题累计"))
    breakdown = row.get("intervention_breakdown") or {}
    for key, label, color in (
        ("clarification", "澄清", _GOLD),
        ("course_correction", "纠偏", _RED),
        ("context_supplement", "补上下文", _BLUE),
        ("brainstorming", "方案讨论", _PURPLE),
    ):
        count = int(breakdown.get(key, 0) or 0)
        if count:
            tags.append((f"{label} {count}", color, f"记录到 {count} 轮{label}"))
    if not tags:
        tags.append(("暂无事实型卡点", _GREEN, "未记录工作流缺口、采集等待、验收缺口、返工、修正、审查问题或分类介入"))
    return tags


def _attention_tags(row: dict) -> str:
    tags = _attention_tag_items(row)
    return "".join(
        f'<span class="helixharness-focus-tag" title="{_esc(title)}" style="background:{color}1f;color:{color};">{_esc(text)}</span>'
        for text, color, title in tags[:3]
    )


def _summary_sentence(row: dict, subject: str) -> str:
    feature_count = int(row.get("feature_count", 0) or 0)
    rework = int(row.get("rework", 0) or 0)
    findings = int(row.get("findings", 0) or 0)
    pending = int(row.get("pending_entries", 0) or 0)
    verify = _verify_text(row)
    if rework or findings:
        return f"{subject}覆盖 {feature_count} 个需求；记录返工 {rework} 次、审查问题 {findings} 个。"
    if pending:
        return f"{subject}覆盖 {feature_count} 个需求；{pending} 条阶段数据仍在采集中。"
    return f"{subject}覆盖 {feature_count} 个需求；验收通过 {verify}，未记录返工或审查问题。"


def _mini_metric(value: Any, label: str, tip_key: str = "", raw: bool = False, hint: str = "") -> str:
    label_html = _tip(label, tip_key) if tip_key else _esc(label)
    value_html = str(value) if raw else _esc(value)
    hint_html = f'<small>{_esc(hint)}</small>' if hint else ""
    value_block = f'<div class="helixharness-mini-value raw">{value_html}</div>' if raw else f'<b>{value_html}</b>'
    return f'<div class="helixharness-mini"><div class="helixharness-mini-label">{label_html}</div>{value_block}{hint_html}</div>'


def _compact_command_counts(commands: dict, limit: int = 10) -> str:
    active = [(cmd, int(count or 0)) for cmd, count in (commands or {}).items() if int(count or 0)]
    if not active:
        return "暂无命令事件"
    active.sort(key=lambda item: (PROJECT_COMMANDS.index(item[0]) if item[0] in PROJECT_COMMANDS else 999, item[0]))
    shown = " · ".join(f"/helix:{cmd} {count}" for cmd, count in active[:limit])
    if len(active) > limit:
        shown += f" · +{len(active) - limit}"
    return shown


def _inline_adoption_html(adoption: dict | None) -> str:
    if not adoption:
        return ""
    if int(adoption.get("total_events", 0) or 0) == 0 and not any(int(v or 0) for v in (adoption.get("commands") or {}).values()):
        return ""
    events = int(adoption.get("total_events", 0) or 0)
    actors = int(adoption.get("unique_actor_count", 0) or 0)
    feature_logs = int(adoption.get("feature_logs_count", 0) or 0)
    last_seen = str(adoption.get("last_seen_at") or "")[:16] or "—"
    commands = _compact_command_counts(adoption.get("commands") or {})
    return (
        f'<div class="helixharness-adoption-inline">'
        f'<span>事件 {events}</span>'
        f'<span>用户 {actors}</span>'
        f'<span>feature logs {feature_logs}</span>'
        f'<span>最近 {last_seen}</span>'
        f'<code>{_esc(commands)}</code>'
        f'</div>'
    )


def _feature_detail_rows(features: list[dict]) -> str:
    if not features:
        return '<div class="helixharness-empty-mini">暂无需求明细。</div>'
    body = ""
    for idx, f in enumerate(sorted(features, key=lambda x: (x["project_name"], x["feature"])), start=1):
        usage = f.get("resource_usage") or {}
        delta = f.get("code_delta") or {}
        bug_fix_count = int(f.get("bug_fix_count", 0) or 0)
        bug_fix_meta = f'<span class="helixharness-muted-pill">Bug Fix {bug_fix_count} 次</span>' if bug_fix_count else ""
        compact_meta = (
            f'{_status_badge("archived" if f["archive_completed"] else f["status"])}'
            f'{_bool_badge(f["verify_passed"], "验收通过", "验收失败", "未验收")}'
            f'{bug_fix_meta}'
            f'<span class="helixharness-muted-pill">当前 {_stage_label(f["current_stage"])}</span>'
        )
        detail_metrics = (
            f'<div class="helixharness-mini-row helixharness-feature-mini">'
            f'{_mini_metric(_status_badge("archived" if f["archive_completed"] else f["status"]), "状态", raw=True)}'
            f'{_mini_metric(_bool_badge(f["verify_passed"], "通过", "失败", "—"), "验收", "verify_pass", raw=True)}'
            f'{_mini_metric(_bool_badge(f["evidence_complete"], "依据完整", "依据不足", "未采集"), "验收依据", "evidence", raw=True)}'
            f'{_mini_metric(f.get("review_findings_count", 0) or 0, "审查问题", "findings")}'
            f'{_mini_metric(_user_interaction_value(f.get("user_interactions")), "用户交互轮次", "user_interactions", hint=_user_interaction_hint(f.get("user_interactions")))}'
            f'{_mini_metric(_loop_rate(int(f.get("loop_count", 0) or 0), int(f.get("loop_total", 0) or 0)), "Loop 使用", "loop_usage", hint=_loop_hint(f))}'
            f'{_mini_metric(_fmt_duration(f.get("total_duration_sec")), "总耗时", "duration")}'
            f'{_mini_metric(_fmt_tokens(usage.get("total_tokens")), "Token", "resource_usage", hint=_resource_summary_line(usage))}'
            f'{_mini_metric(_fmt_code_delta(delta), "代码变更", "code_delta", hint=_code_summary_line(delta))}'
            f'{_mini_metric(f["rework_count"], "返工", "rework")}'
            f'{_mini_metric(f.get("revision_count", 0), "跨阶段修正", "revision", hint=_revision_summary(f))}'
            f'{_mini_metric(_stage_label(f["current_stage"]), "阶段")}'
            f'</div>'
        )
        body += (
            f'<details class="helixharness-feature-drill"><summary class="helixharness-feature-summary">'
            f'<div class="helixharness-drill-title"><div class="helixharness-feature-title-line"><span class="helixharness-level-badge">需求 {idx}</span><b>{_esc(f["feature"])}</b></div><code>{_esc(f["project_name"])}</code><div class="helixharness-feature-summary-tags">{compact_meta}</div></div>'
            f'<span class="helixharness-open-hint" data-open="查看需求明细" data-close="收起需求明细"></span>{detail_metrics}</summary>'
            f'<div class="helixharness-feature-body">{render_feature_timeline_html({"feature": f["feature"], "project": f["project"], "status": f["status"], "entries": f["entries"], "feature_summary": {}}, archive_changes=f.get("archive_changes"))}</div></details>'
        )
    return f'<div class="helixharness-feature-list">{body}</div>'


def _feature_detail_section(features: list[dict], title: str = "需求明细") -> str:
    count = len(features)
    return (
        f'<details class="helixharness-nested-drill"><summary>'
        f'<span><em>第二层</em>{_esc(title)}</span>'
        f'<small>{count} 个需求。这里先展示需求目录，选择某个需求后再查看明细。</small>'
        f'<b data-open="展开需求列表" data-close="收起需求列表"></b>'
        f'</summary><div class="helixharness-nested-body">{_feature_detail_rows(features)}</div></details>'
    )


def _project_detail_rows(rows: list[dict]) -> str:
    if not rows:
        return '<div class="helixharness-empty-mini">暂无项目明细。</div>'
    body = ""
    for r in sorted(rows, key=lambda x: x["project_name"]):
        archive_text = f'{r["archived_count"]}/{r["feature_count"]}'
        progress = (
            f'<div class="helixharness-cell-title">归档 {_esc(archive_text)}</div>'
            f'<div class="helixharness-cell-sub">验收 {_esc(_verify_text(r))} · 需求 {r["feature_count"]}</div>'
        )
        friction = (
            f'<div class="helixharness-cell-title">用户交互轮次 {_user_interaction_value(r.get("user_interactions"))}</div>'
            f'<div class="helixharness-cell-sub">{_user_interaction_hint(r.get("user_interactions"))} · Loop {_loop_hint(r)} · 返工 {r["rework"]} · 修正 {r.get("revisions", 0)} · 问题 {r["findings"]}</div>'
        )
        usage = r.get("resource_usage") or {}
        delta = r.get("code_delta") or {}
        output = (
            f'<div class="helixharness-cell-title">{_fmt_tokens(usage.get("total_tokens"))} tokens · {_fmt_code_delta(delta)}</div>'
            f'<div class="helixharness-cell-sub">{_resource_summary_line(usage)}<br>{_code_summary_line(delta)}</div>'
        )
        body += (
            '<tr>'
            f'<td><b>{_esc(r["project_name"])}</b></td>'
            f'<td>{progress}</td>'
            f'<td>{friction}</td>'
            f'<td>{output}</td>'
            f'<td><div class="helixharness-focus-tags">{_attention_tags(r)}</div>{_inline_adoption_html(r.get("adoption"))}</td>'
            '</tr>'
        )
    return (f'<table class="helixharness-table helixharness-detail-table"><thead><tr>'
            f'<th>项目</th><th>{_tip("进展", "archived")}</th><th>{_tip("用户交互轮次", "user_interactions")}</th><th>{_tip("资源与代码", "code_delta")}</th><th>{_tip("事实型关注点", "focus_tags")}</th>'
            f'</tr></thead><tbody>{body}</tbody></table>')


def _user_detail_rows(rows: list[dict]) -> str:
    if not rows:
        return '<div class="helixharness-empty-mini">暂无用户明细。</div>'
    body = ""
    for r in sorted(rows, key=lambda x: x["user_id"]):
        archive_text = f'{r["archived_count"]}/{r["feature_count"]}'
        progress = (
            f'<div class="helixharness-cell-title">归档 {_esc(archive_text)}</div>'
            f'<div class="helixharness-cell-sub">验收 {_esc(_verify_text(r))} · 需求 {r["feature_count"]}</div>'
        )
        friction = (
            f'<div class="helixharness-cell-title">用户交互轮次 {_user_interaction_value(r.get("user_interactions"))}</div>'
            f'<div class="helixharness-cell-sub">{_user_interaction_hint(r.get("user_interactions"))} · Loop {_loop_hint(r)} · 返工 {r["rework"]} · 修正 {r.get("revisions", 0)} · 问题 {r["findings"]}</div>'
        )
        usage = r.get("resource_usage") or {}
        delta = r.get("code_delta") or {}
        output = (
            f'<div class="helixharness-cell-title">{_fmt_tokens(usage.get("total_tokens"))} tokens · {_fmt_code_delta(delta)}</div>'
            f'<div class="helixharness-cell-sub">{_resource_summary_line(usage)}<br>{_code_summary_line(delta)}</div>'
        )
        body += (
            '<tr>'
            f'<td><b>{_esc(_short(r["user_id"], 16))}</b><br>{_conf_badge(r["confidence"])}</td>'
            f'<td>{progress}</td>'
            f'<td>{friction}</td>'
            f'<td>{output}</td>'
            f'<td><div class="helixharness-focus-tags">{_attention_tags(r)}</div>{_inline_adoption_html(r.get("adoption"))}</td>'
            '</tr>'
        )
    return (f'<table class="helixharness-table helixharness-detail-table"><thead><tr>'
            f'<th>用户</th><th>{_tip("进展", "archived")}</th><th>{_tip("用户交互轮次", "user_interactions")}</th><th>{_tip("资源与代码", "code_delta")}</th><th>{_tip("事实型关注点", "focus_tags")}</th>'
            f'</tr></thead><tbody>{body}</tbody></table>')


def _summarize_user_rows(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for r in rows:
        user_id = r["user_id"]
        row = grouped.setdefault(user_id, {
            "user_id": user_id,
            "user_name": r.get("user_name") or "未命名用户",
            "confidence": r.get("confidence", "unknown"),
            "projects": set(),
            "project_rows": [],
            "feature_count": 0,
            "archived_count": 0,
            "verify_passed": 0,
            "verify_total": 0,
            "evidence_complete": 0,
            "evidence_total": 0,
            "loop_count": 0,
            "loop_total": 0,
            "workflow_completed": 0,
            "telemetry_finalized": 0,
            "pending_entries": 0,
            "interventions": 0,
            "deterministic_interventions": 0,
            "semantic_interventions": 0,
            "intervention_breakdown": {key: 0 for key in BREAKDOWN_KEYS},
            "rework": 0,
            "revisions": 0,
            "rollback": 0,
            "findings": 0,
            "resource_usages": [],
            "interaction_usages": [],
        })
        if row["user_name"] == "未命名用户" and r.get("user_name"):
            row["user_name"] = r["user_name"]
        row["projects"].add(r["project_id"])
        row["project_rows"].append(r)
        for key in [
            "feature_count", "archived_count", "verify_passed", "verify_total",
            "evidence_complete", "evidence_total", "loop_count", "loop_total",
            "workflow_completed", "telemetry_finalized", "pending_entries",
            "interventions", "deterministic_interventions", "semantic_interventions",
            "rework", "revisions", "rollback", "findings",
        ]:
            row[key] += int(r.get(key, 0) or 0)
        for key in BREAKDOWN_KEYS:
            row["intervention_breakdown"][key] += int((r.get("intervention_breakdown") or {}).get(key, 0) or 0)
        row["resource_usages"].append(r.get("resource_usage") or {})
        row["interaction_usages"].append(r.get("user_interactions") or {})
    result = []
    for row in grouped.values():
        project_count = len(row["projects"])
        resource_usage = _aggregate_resource_usages(row["resource_usages"], "user")
        result.append({
            **{k: v for k, v in row.items() if k not in {"projects", "resource_usages", "interaction_usages"}},
            "resource_usage": resource_usage,
            "user_interactions": _merge_user_interactions(row["interaction_usages"]),
            "code_delta": _unknown_code_delta(),
            "project_count": project_count,
            "avg_interventions": (
                round(row["interventions"] / row["feature_count"], 1)
                if row["feature_count"]
                else 0
            ),
        })
    result.sort(key=lambda x: x["user_id"])
    return result


def _render_user_view(rows: list[dict], features: list[dict]) -> str:
    users = _summarize_user_rows(rows)
    if not users:
        return '<div class="helixharness-empty">暂无用户聚合数据。</div>'
    cards = ""
    for u in users:
        user_features = [f for f in features if u["user_id"] in f.get("actors", [])]
        summary = _summary_sentence(u, "该用户")
        usage = u.get("resource_usage") or {}
        delta = u.get("code_delta") or {}
        cards += (
            f'<details class="helixharness-drill"><summary class="helixharness-drill-summary">'
            f'<div class="helixharness-drill-title"><div class="helixharness-identity-line"><code>{_esc(_short(u["user_id"], 18))}</code>{_conf_badge(u["confidence"])}</div>'
            f'<div class="helixharness-row-summary">{_esc(summary)}</div>{_inline_adoption_html(u.get("adoption"))}<div class="helixharness-focus-tags">{_attention_tags(u)}</div></div>'
            f'<div class="helixharness-mini-row">'
            f'{_mini_metric(u["project_count"], "参与项目", "project_total", hint="覆盖范围")}'
            f'{_mini_metric(u["feature_count"], "处理需求", "features", hint="工作量")}'
            f'{_mini_metric(_user_interaction_value(u.get("user_interactions")), "用户交互轮次", "user_interactions", hint=_user_interaction_hint(u.get("user_interactions")))}'
            f'{_mini_metric(_loop_rate(int(u.get("loop_count", 0) or 0), int(u.get("loop_total", 0) or 0)), "Loop 使用", "loop_usage", hint=_loop_hint(u))}'
            f'{_mini_metric(_fmt_tokens(usage.get("total_tokens")), "Token 消耗", "resource_usage", hint=_resource_summary_line(usage))}'
            f'{_mini_metric(_fmt_code_delta(delta), "代码变更", "code_delta", hint=_code_summary_line(delta))}'
            f'{_mini_metric(_verify_text(u), "验收通过", "verify_pass", hint="通过/总数")}'
            f'{_mini_metric(_evidence_text(u), "验收依据", "evidence", hint="完整/已检查")}'
            f'{_mini_metric(u["rework"], "返工次数", "rework", hint="越少越好")}'
            f'{_mini_metric(u.get("revisions", 0), "跨阶段修正", "revision", hint=_revision_summary(u))}'
            f'</div><span class="helixharness-open-hint" data-open="查看项目明细" data-close="收起项目明细"></span></summary>'
            f'<div class="helixharness-drill-body"><div class="helixharness-subtitle">项目明细 {_tip("说明", "project_detail")}</div>{_project_detail_rows(u["project_rows"])}'
            f'{_feature_detail_section(user_features)}</div></details>'
        )
    return cards


def _render_project_view(rows: list[dict], user_project_rows: list[dict], features: list[dict]) -> str:
    if not rows:
        return '<div class="helixharness-empty">暂无项目聚合数据。</div>'
    cards = ""
    for p in rows:
        project_user_rows = [r for r in user_project_rows if r["project_id"] == p["project_id"]]
        project_features = [f for f in features if f["project_id"] == p["project_id"]]
        summary = _summary_sentence(p, "该项目")
        usage = p.get("resource_usage") or {}
        delta = p.get("code_delta") or {}
        cards += (
            f'<details class="helixharness-drill"><summary class="helixharness-drill-summary">'
            f'<div class="helixharness-drill-title"><div class="helixharness-identity-line"><b>{_esc(p["project_name"])}</b></div>'
            f'<div class="helixharness-row-summary">{_esc(summary)}</div>{_inline_adoption_html(p.get("adoption"))}<div class="helixharness-focus-tags">{_attention_tags(p)}</div></div>'
            f'<div class="helixharness-mini-row">'
            f'{_mini_metric(p["user_count"], "参与用户", "users", hint="协作规模")}'
            f'{_mini_metric(p["feature_count"], "覆盖需求", "features", hint="需求量")}'
            f'{_mini_metric(_user_interaction_value(p.get("user_interactions")), "用户交互轮次", "user_interactions", hint=_user_interaction_hint(p.get("user_interactions")))}'
            f'{_mini_metric(_loop_rate(int(p.get("loop_count", 0) or 0), int(p.get("loop_total", 0) or 0)), "Loop 使用", "loop_usage", hint=_loop_hint(p))}'
            f'{_mini_metric(_fmt_tokens(usage.get("total_tokens")), "Token 消耗", "resource_usage", hint=_resource_summary_line(usage))}'
            f'{_mini_metric(_fmt_code_delta(delta), "代码变更", "code_delta", hint=_code_summary_line(delta))}'
            f'{_mini_metric(_verify_text(p), "验收通过", "verify_pass", hint="通过/总数")}'
            f'{_mini_metric(_evidence_text(p), "验收依据", "evidence", hint="完整/已检查")}'
            f'{_mini_metric(p["rework"], "返工次数", "rework", hint="越少越好")}'
            f'{_mini_metric(p.get("revisions", 0), "跨阶段修正", "revision", hint=_revision_summary(p))}'
            f'</div><span class="helixharness-open-hint" data-open="查看用户明细" data-close="收起用户明细"></span></summary>'
            f'<div class="helixharness-drill-body"><div class="helixharness-subtitle">用户明细</div>{_user_detail_rows(project_user_rows)}'
            f'{_feature_detail_section(project_features)}</div></details>'
        )
    return cards


def _view_mode_key(view_mode: str | None) -> str:
    value = (view_mode or "").strip().lower()
    if value in {"按项目维度", "项目维度", "project", "projects"}:
        return "project"
    return "user"


def _ratio_text(done: int, total: int) -> str:
    return f"{done}/{total}" if total else "—"


def _dimension_cards(summary: dict, rows: list[dict], mode_key: str) -> str:
    interactions = _merge_user_interactions([row.get("user_interactions") or {} for row in rows])
    if mode_key == "project":
        project_count = len(rows)
        feature_count = sum(int(r.get("feature_count", 0) or 0) for r in rows)
        user_count = sum(int(r.get("user_count", 0) or 0) for r in rows)
        archived = sum(int(r.get("archived", 0) or 0) for r in rows)
        verify_passed = sum(int(r.get("verify_passed", 0) or 0) for r in rows)
        verify_total = sum(int(r.get("verify_total", 0) or 0) for r in rows)
        rework = sum(int(r.get("rework", 0) or 0) for r in rows)
        findings = sum(int(r.get("findings", 0) or 0) for r in rows)
        revisions = sum(int(r.get("revisions", 0) or 0) for r in rows)
        pending = sum(int(r.get("pending_entries", 0) or 0) for r in rows)
        loop_count = sum(int(r.get("loop_count", 0) or 0) for r in rows)
        loop_total = sum(int(r.get("loop_total", 0) or 0) for r in rows)
        loop_success = sum(int(r.get("loop_success", 0) or 0) for r in rows)
        loop_turns = sum(int(r.get("loop_turns", 0) or 0) for r in rows)
        resource_usage = _aggregate_resource_usages(
            [r.get("resource_usage") or {} for r in rows],
            "project_dimension",
        )
        code_delta = _sum_numeric_dicts([r.get("code_delta") or {} for r in rows], CODE_DELTA_KEYS)
        return "".join([
            _card(project_count, "项目数", _BLUE, tip_key="project_total"),
            _card(feature_count, "需求数", _ACCENT, tip_key="feature_total"),
            _card(user_count, "参与人次", _PURPLE, "按项目累计", "users"),
            _card(_user_interaction_value(interactions), "用户交互轮次", _BLUE, _user_interaction_hint(interactions), "user_interactions"),
            _card(_loop_rate(loop_count, loop_total), "Loop 使用", _BLUE, _loop_feature_summary(loop_count, loop_total, loop_success, loop_turns), "loop_usage"),
            _card(_fmt_tokens(resource_usage.get("total_tokens")), "Token 消耗", _BLUE, _resource_summary_line(resource_usage), "resource_usage"),
            _card(_fmt_code_delta(code_delta), "代码变更", _ACCENT, _code_summary_line(code_delta), "code_delta"),
            _card(sum(int(r.get("evidence_complete", 0) or 0) for r in rows), "验收依据完整", _GREEN, f"已检查 {sum(int(r.get('evidence_total', 0) or 0) for r in rows)}", "evidence"),
            _card(_ratio_text(archived, feature_count), "已归档", _GRAY, tip_key="archived"),
            _card(_ratio_text(verify_passed, verify_total), "验收通过", _GREEN, tip_key="verify_pass"),
            _card(rework, "返工次数", _RED, tip_key="rework"),
            _card(revisions, "跨阶段修正", _PURPLE, f"采集中 {pending}", "revision"),
            _card(findings, "审查问题", _RED, tip_key="findings"),
        ])

    row_count = len(rows)
    user_count = len({r.get("user_id") for r in rows})
    feature_count = sum(int(r.get("feature_count", 0) or 0) for r in rows)
    archived = sum(int(r.get("archived_count", 0) or 0) for r in rows)
    verify_passed = sum(int(r.get("verify_passed", 0) or 0) for r in rows)
    verify_total = sum(int(r.get("verify_total", 0) or 0) for r in rows)
    rework = sum(int(r.get("rework", 0) or 0) for r in rows)
    findings = sum(int(r.get("findings", 0) or 0) for r in rows)
    revisions = sum(int(r.get("revisions", 0) or 0) for r in rows)
    pending = sum(int(r.get("pending_entries", 0) or 0) for r in rows)
    loop_count = sum(int(r.get("loop_count", 0) or 0) for r in rows)
    loop_total = sum(int(r.get("loop_total", 0) or 0) for r in rows)
    loop_success = sum(int(r.get("loop_success", 0) or 0) for r in rows)
    loop_turns = sum(int(r.get("loop_turns", 0) or 0) for r in rows)
    resource_usage = _aggregate_resource_usages(
        [r.get("resource_usage") or {} for r in rows],
        "user_dimension",
    )
    code_delta = _sum_numeric_dicts([r.get("code_delta") or {} for r in rows], CODE_DELTA_KEYS)
    return "".join([
        _card(user_count, "用户数", _PURPLE, tip_key="user_total"),
        _card(row_count, "用户项目数", _BLUE, "用户 × 项目", "project_total"),
        _card(feature_count, "覆盖需求", _ACCENT, "按用户项目累计", "features"),
        _card(_user_interaction_value(interactions), "用户交互轮次", _BLUE, _user_interaction_hint(interactions), "user_interactions"),
        _card(_loop_rate(loop_count, loop_total), "Loop 使用", _BLUE, _loop_feature_summary(loop_count, loop_total, loop_success, loop_turns), "loop_usage"),
        _card(_fmt_tokens(resource_usage.get("total_tokens")), "Token 消耗", _BLUE, _resource_summary_line(resource_usage), "resource_usage"),
        _card(_fmt_code_delta(code_delta), "代码变更", _ACCENT, _code_summary_line(code_delta), "code_delta"),
        _card(sum(int(r.get("evidence_complete", 0) or 0) for r in rows), "验收依据完整", _GREEN, f"已检查 {sum(int(r.get('evidence_total', 0) or 0) for r in rows)}", "evidence"),
        _card(_ratio_text(archived, feature_count), "已归档", _GRAY, tip_key="archived"),
        _card(_ratio_text(verify_passed, verify_total), "验收通过", _GREEN, tip_key="verify_pass"),
        _card(rework, "返工次数", _RED, tip_key="rework"),
        _card(revisions, "跨阶段修正", _PURPLE, f"采集中 {pending}", "revision"),
        _card(findings, "审查问题", _RED, tip_key="findings"),
    ])


def _metric_guide_html() -> str:
    items = [
        (
            "数据主输入",
            "接受 HelixHarness Ledger 1.0 与已关联异步 Supplement 的物化数据；缺少必填字段的日志拒绝进入聚合。",
        ),
        ("用户交互轮次", "按阶段记录累计已采集的用户消息轮次，包含“继续/OK”等确认；不等同于有效介入、澄清或返工。部分采集显示小计与覆盖数，未采集不计为 0。"),
        ("Token 消耗", "统计模型输入、输出、缓存读写 Token 的总消耗，只看资源用量，不计算金额。"),
        ("归因和污染风险", "Token 优先来自平台结构化 usage 或精确 session 窗口；代码变更来自需求 Git 区间并优先限制 produced_files。范围不足时会降低 confidence 或明确 unavailable。"),
        ("验收事实", "分别展示验收通过数、验收依据完整数和审查问题数；每项都能回到 eval-log 的结构化字段，不合成质量分。"),
        ("跨阶段修正", "HelixHarness 不回写旧阶段；后续阶段修改上一更新内容时，记录在当前阶段的 workflow.revises_previous_stage，并标明 revised_stage_kind 与 revision_reason。语义性修正同时计入返工。"),
        ("Loop 使用", "仅展示 entries.invocation.loop 与 loop_max_turns 的原始配置事实，不推断成功率、实际轮次或终止原因。"),
        ("验收依据覆盖率", "有明确验收依据的需求数 / 已检查验收依据的需求数。它回答的是：这个需求说验收通过或失败时，HelixHarness 记录里能不能找到支撑结论的测试命令、执行结果和说明。"),
        ("验收依据来源", "原始记录通常在 docs/specs/active/{feature}/verify-report.md、docs/specs/active/{feature}/verify-report.md 和 tasks.md；归档后会移动到 docs/specs/completed/{feature}/。WebUI 读取的是 docs/eval/{feature}/eval-log.json 中的结构化结果。"),
        ("返工率", "返工阶段数 / 需求数，越高说明前序产物被明显调整、重做或修复的比例越高。"),
        ("审查问题", "直接累计 review_findings_count；不按密度折算、不截断、不参与综合评分。"),
        ("介入分类", "直接累计方案讨论、需求澄清、方向纠偏和上下文补充的轮次；不设置好坏阈值。"),
        ("数据状态", "分别展示统计覆盖、finalization、资源采集和语义分类状态，不再合成单一综合置信度。"),
        ("关注标签", "只展示工作流缺口、采集等待、验收缺口、返工、跨阶段修正、审查问题和各类介入的实际计数。"),
        ("介入标签", "方案讨论表示用户参与方案取舍；需求澄清表示补充边界或约束；方向纠偏表示指出方向不对或要求重做；上下文补充表示提供模型缺失的业务/代码背景。"),
        ("使用情况读法", "看命令事件与流程步骤判断实际使用范围；看耗时、介入和返工判断过程；看验收、验收依据和审查问题判断交付事实。"),
    ]
    body = "".join(f'<div class="helixharness-guide-item"><b>{_esc(title)}</b><span>{_esc(text)}</span></div>' for title, text in items)
    return f'<details class="helixharness-guide"><summary>指标口径与读法</summary><div class="helixharness-guide-grid">{body}</div></details>'


def _html_id(prefix: str, value: Any, idx: int) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-").lower()
    return f"{prefix}-{idx}-{text or 'item'}"


def _feature_table_rows(
    features: list[dict],
    ai_insights: dict | None = None,
    *,
    id_prefix: str = "helixharness-feature-detail",
    split_identity: bool = False,
) -> tuple[str, str]:
    rows = []
    detail_pages = []
    for idx, f in enumerate(sorted(features, key=lambda x: (
        _newest_log_sort_key(x.get("log_time")), x["feature"], x["project_name"],
    )), start=1):
        detail_id = _html_id(id_prefix, f["feature"], idx)
        usage = f.get("resource_usage") or {}
        actors = ", ".join(
            item.get("user_name", "未命名用户")
            for item in f.get("installations", [])
        ) or "未识别用户"
        if split_identity:
            identity_cells = (
                f'<td><b>{_esc(f["project_name"])}</b></td>'
                f'<td><div class="helixharness-feature-cell-title"><b>{_esc(f["feature"])}</b></div>'
                f'{_coverage_badge(f.get("collection_coverage", "unknown"))}</td>'
                f'<td>{_esc(actors)}</td>'
            )
        else:
            identity_cells = (
                f'<td><div class="helixharness-feature-cell-title"><b>{_esc(f["feature"])}</b></div>'
                f'<span>{_esc(f["project_name"])} · {_esc(actors)}</span><br>'
                f'{_coverage_badge(f.get("collection_coverage", "unknown"))}</td>'
            )
        row = (
            "<tr>"
            f'{identity_cells}'
            f'<td class="helixharness-log-time-cell">{_log_time_html(f.get("log_time", ""))}</td>'
            f'<td><b>{_esc(_progress_text(f))}</b><br>{_progress_hint_html(f)}</td>'
            f'<td><b class="helixharness-num">{_user_interaction_value(f.get("user_interactions"))}</b><br><span>{_user_interaction_hint(f.get("user_interactions"))}</span></td>'
            f'<td class="helixharness-num">{_esc(_fmt_duration(f.get("total_duration_sec")))}</td>'
            f'<td><b class="helixharness-num">{_esc(_fmt_tokens(usage.get("total_tokens")))}</b><br>{_resource_badge(usage)}</td>'
            f'<td><a class="helixharness-action" href="#{detail_id}">查看详情</a></td>'
            "</tr>"
        )
        rows.append(row)
        detail_pages.append(_feature_detail_page(f, detail_id, ai_insights))
    return "".join(rows), "".join(detail_pages)


def _feature_detail_page(f: dict, detail_id: str, ai_insights: dict | None = None) -> str:
    ai_detail_id = f"{detail_id}-ai"
    usage = f.get("resource_usage") or {}
    code_delta = f.get("code_delta") or {}
    code_delta_text = _fmt_code_delta(code_delta)
    metrics = "".join([
        _mini_metric(
            _progress_text(f),
            "流程进度 / 最近阶段",
            "funnel",
            hint=_progress_hint(f),
        ),
        _mini_metric(_fmt_tokens(usage.get("total_tokens")), "Token", "resource_usage", hint=_resource_summary_line(usage)),
        _mini_metric(_fmt_duration(f.get("total_duration_sec")), "阶段累计耗时", "duration", hint=_stage_duration_summary(f)),
        _mini_metric(
            "未采集" if code_delta_text == "—" else code_delta_text,
            _code_delta_label(code_delta),
            "code_delta",
            hint=_code_summary_line(code_delta),
        ),
    ])
    timeline = render_feature_timeline_html({"feature": f["feature"], "project": f["project"], "status": f["status"], "entries": f["entries"], "feature_summary": {}}, archive_changes=f.get("archive_changes"))
    result = (
        f'<section id="{detail_id}" class="helixharness-detail-page">'
        f'<div class="helixharness-detail-page-head"><div><b>{_esc(f["feature"])}</b><span>需求详情 · {_esc(f["project_name"])} </span>'
        f'<span class="helixharness-runtime-summary">{_esc(_feature_runtime_summary(f))}</span></div><a href="#helixharness-usage-top">返回总览</a></div>'
        f'<div class="helixharness-mini-row helixharness-detail-metrics">{metrics}</div>'
        f'{_feature_ai_entry_html(f, ai_detail_id)}'
        f'{_stage_duration_share_html([f])}'
        f'<div class="helixharness-panel"><div class="helixharness-section-title">阶段过程明细</div>{timeline}</div>'
        f'</section>'
        f'{_feature_ai_detail_page(f, ai_detail_id, detail_id, ai_insights)}'
    )
    return result


def _combined_user_feature_table_html(
    users: list[dict],
    features: list[dict],
    ai_insights: dict | None = None,
) -> tuple[str, str]:
    rows: list[str] = []
    detail_pages: list[str] = []
    user_groups = []
    for user in users:
        user_id = str(user.get("user_id") or "")
        user_features = [feature for feature in features if user_id in feature.get("actors", [])]
        latest = _latest_log_time([f.get("log_time") for f in user_features])
        user_groups.append((user, user_features, latest))
    user_groups.sort(key=lambda item: (
        _newest_log_sort_key(item[2]), str(item[0].get("user_name") or ""), str(item[0].get("user_id") or ""),
    ))
    for index, (user, user_features, latest) in enumerate(user_groups, start=1):
        user_id = str(user.get("user_id") or "")
        user_name = str(user.get("user_name") or "未命名用户")
        child_rows, child_pages = _feature_table_rows(
            user_features,
            ai_insights,
            id_prefix=f"helixharness-user-{index}-feature-detail",
            split_identity=True,
        )
        archived = sum(int(feature.get("archive_completed") is True) for feature in user_features)
        user_id_html = (
            f'<code class="helixharness-user-id">{_esc(_short(user_id, 24))}</code>'
            if user_id and user_id != user_name
            else ""
        )
        rows.append(
            '<tr class="helixharness-requirement-row"><td>'
            '<details class="helixharness-requirement-group helixharness-user-group">'
            '<summary><span class="helixharness-requirement-identity helixharness-user-identity">'
            f'<span class="helixharness-requirement-label helixharness-user-label">用户</span><b>{_esc(user_name)}</b>{user_id_html}</span>'
            f'{_log_time_html(latest, latest=True)}'
            f'<span class="helixharness-requirement-counts">项目 {len(user_features)} · 归档 {archived}/{len(user_features)}</span>'
            '<span class="helixharness-requirement-toggle" data-open="展开项目" data-close="收起项目"></span>'
            '</summary>'
            f'<div class="helixharness-requirement-body">{_feature_table_html(child_rows, main=False, split_identity=True)}</div>'
            '</details></td></tr>'
        )
        detail_pages.append(child_pages)

    return (
        _feature_table_html(
            "".join(rows),
            extra_class="helixharness-combined-table helixharness-user-combined-table",
            show_header=False,
        ),
        "".join(detail_pages),
    )


def _feature_table_html(
    rows: str,
    *,
    main: bool = True,
    first_column: str = "需求",
    extra_class: str = "",
    split_identity: bool = False,
    show_header: bool = True,
) -> str:
    if not rows:
        return '<div class="helixharness-empty-mini">暂无需求数据。</div>'
    table_class = "helixharness-main-table" if main else "helixharness-requirement-child-table"
    classes = f"helixharness-table {table_class} {extra_class}".strip()
    identity_headers = (
        "<th>代码库</th><th>需求名</th><th>开发人</th>"
        if split_identity
        else f'<th>{_esc(first_column)}</th>'
    )
    colgroup = (
        '<colgroup>'
        '<col class="helixharness-col-repository"><col class="helixharness-col-requirement"><col class="helixharness-col-developer">'
        '<col class="helixharness-col-log-time">'
        '<col class="helixharness-col-progress"><col class="helixharness-col-intervention"><col class="helixharness-col-duration">'
        '<col class="helixharness-col-token"><col class="helixharness-col-action">'
        '</colgroup>'
        if split_identity
        else ""
    )
    header = (
        '<thead><tr>'
        f'{identity_headers}<th title="日志条目中的最晚实际时间，按北京时间显示">日志时间</th>'
        f'<th>{_tip("流程进度 / 最近阶段", "status_stage")}</th>'
        f'<th>{_tip("用户交互轮次", "user_interactions")}</th><th>{_tip("阶段累计耗时", "duration")}</th><th>{_tip("Token", "resource_usage")}</th><th></th>'
        '</tr></thead>'
        if show_header
        else ""
    )
    return (
        f'<div class="helixharness-table-wrap"><table class="{classes}">{colgroup}{header}<tbody>{rows}</tbody></table></div>'
    )


def _display_rules_html() -> str:
    items = [
        (
            "流程进度 / 最近阶段",
            "归档事实成立时按 HelixHarness 3/3 展示；阶段日志不齐时只标记数据缺失，不影响流程进度。",
        ),
        (
            "数据状态",
            "只表示异步明细是否齐全；异步明细从 eval-log 被发现并关联时开始生效。",
        ),
        (
            "用户交互轮次",
            "累计已采集的用户消息轮次，包含确认；部分采集显示小计，未采集不计为 0，不作为介入或返工判断。",
        ),
        (
            "AI 解读",
            "首页 AI 看全部项目的整体情况；项目里的 AI 深度解读只看当前项目。",
        ),
    ]
    body = "".join(
        f'<div class="helixharness-rule-item"><b>{_esc(title)}</b><span>{_esc(text)}</span></div>'
        for title, text in items
    )
    return (
        '<div class="helixharness-panel helixharness-rules-panel"><div class="helixharness-section-title">页面口径</div>'
        f'<div class="helixharness-rules-grid">{body}</div></div>'
    )


def _ai_placeholder_html(
    title: str,
    scope: str,
    notice: str | None = None,
    *, stale: bool = False,
) -> str:
    empty_notice = notice or "尚未生成 AI 解读。"
    heading = "数据已更新，解读待刷新" if stale else "尚未生成 AI 解读"
    badge = "待刷新" if stale else "尚未生成"
    return (
        f'<div class="helixharness-panel helixharness-ai-panel helixharness-ai-empty">'
        f'<div class="helixharness-ai-head"><div><div class="helixharness-section-title">{_esc(title)}</div>'
        f'<p>{_esc(scope)}</p></div><span>{badge}</span></div>'
        '<div class="helixharness-ai-empty-state">'
        f'<div><b>{heading}</b><span>{_esc(empty_notice)}</span></div>'
        '<a class="helixharness-action" href="/config#ai-analysis">立即生成</a>'
        '</div>'
        f'</div>'
    )


def _ai_missing_notice(ai_insights: dict | None, scope: str) -> str:
    status = str((ai_insights or {}).get("status") or "").strip()
    error = str((ai_insights or {}).get("error") or "").strip()
    target = scope or "AI 解读"
    if not ai_insights:
        return f"{target}尚未生成。"
    if status == "disabled":
        return f"{target}尚未开启，请先确认 AI 配置。"
    if status == "error":
        return f"{target}本次生成失败，请确认服务配置后重新生成。"
    if status == "partial":
        return f"{target}尚未生成，请重新生成。"
    if status == "stale":
        return f"数据已更新，{target}待刷新。"
    if error:
        return f"{target}尚未生成。"
    return f"{target}尚未生成。"


AI_ANSWER_LABELS = [
    ("conclusion", "结论", _ACCENT),
    ("evidence", "关键事实", _BLUE),
    ("blocker", "过程卡点", _RED),
    ("action", "建议动作", _GREEN),
    ("note", "补充说明", _GRAY),
]
TEAM_AI_KEYS = {"conclusion", "blocker", "action"}


def _parse_ai_answer_sections(text: str) -> list[tuple[str, str, str, str]]:
    label_pattern = "结论|关键事实|关键判断|关键证据|证据|过程卡点|主要卡点|卡点|建议动作|补充说明|说明|流程状态|完成效率|细节复盘"
    major_label_pattern = "结论|关键事实|关键判断|关键证据|过程卡点|主要卡点|建议动作|补充说明|流程状态|完成效率|细节复盘"
    raw_text = str(text or "")
    raw_text = re.sub(rf"\*\*\s*({label_pattern})\s*([:：\-—])?\s*\*\*", r"\1\2", raw_text)
    raw_text = re.sub(r"(?m)^\s*(?:[○•\-]\s*)?\*\*\s*$", "", raw_text)
    raw_text = raw_text.replace("**", "")
    raw_text = re.sub(rf"(\d+[.、]\s*)({label_pattern})(?=[^\s:：\-—])", r"\1\2：", raw_text)
    # markdown 标题(## N. 标签) 或 (## 标签) 行尾补冒号, 让后续 inline 解析能命中
    raw_text = re.sub(rf"(?m)^\s*(?:#+\s*)?(?:[○•\-]\s*)?(\d+[.、]\s*)?({major_label_pattern})\s*$", r"\1\2：", raw_text)
    raw_text = re.sub(rf"(?m)^\s*(?:#+\s*)?(?:[○•\-]\s*)?({major_label_pattern})\s*$", r"\1：", raw_text)
    raw_text = re.sub(rf"([。；;])({major_label_pattern})\s*[:：\-—]", r"\1\n\2：", raw_text)
    lines = [line.strip() for line in raw_text.splitlines()]
    sections: dict[str, list[str]] = {key: [] for key, _, _ in AI_ANSWER_LABELS}
    current_key = ""
    label_map = {
        "结论": "conclusion",
        "关键事实": "evidence",
        "关键判断": "evidence",
        "关键证据": "evidence",
        "证据": "evidence",
        "过程卡点": "blocker",
        "主要卡点": "blocker",
        "卡点": "blocker",
        "建议动作": "action",
        "补充说明": "note",
        "说明": "note",
    }
    inline_pattern = re.compile(rf"(?:^|\n|\s+|\d+[.、]\s*)({label_pattern})\s*[:：\-—]\s*")
    matches = list(inline_pattern.finditer(raw_text))
    if len(matches) >= 2:
        for idx, match in enumerate(matches):
            key = label_map.get(match.group(1), "")
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_text)
            value = raw_text[start:end].strip()
            if key and value:
                sections[key].append(value)
        return [
            (key, label, color, " ".join(part for part in sections[key] if part).strip())
            for key, label, color in AI_ANSWER_LABELS
            if " ".join(part for part in sections[key] if part).strip()
        ]

    pattern = re.compile(rf"^(?:\d+[.、]\s*)?({label_pattern})\s*[:：\-—]\s*(.*)$")
    for line in lines:
        if not line:
            continue
        match = pattern.match(line)
        if match:
            current_key = label_map.get(match.group(1), "")
            if current_key and match.group(2).strip():
                sections[current_key].append(match.group(2).strip())
            continue
        if current_key:
            sections[current_key].append(re.sub(r"^\d+[.、]\s*", "", line).strip())
    parsed = []
    for key, label, color in AI_ANSWER_LABELS:
        value = " ".join(part for part in sections[key] if part).strip()
        if value:
            parsed.append((key, label, color, value))
    return parsed


def _format_ai_section_body(value: str) -> str:
    text = str(value or "").strip()
    # 去掉行首的编号（1. 2、 等），但不能误伤小数点（如 4.5）
    # 编号只在行首或分号/换行后出现，且后面必须跟空白或中文
    normalized = re.sub(r"(?:^|(?<=[\n；;]))\s*(?:\d+[.、]|[一二三四五六七八九十][、.])\s*(?=[\u4e00-\u9fffA-Za-z])", "\n", text)
    normalized = re.sub(r"\s*(?:[○•●◦·]|(?<![A-Za-z0-9])[oO](?=\s+))\s*", "\n", normalized)
    # 句号分句：只在中日文句号（。．）后分，英文句点不安全（小数、缩写）
    normalized = re.sub(r"\s*[。．]\s*", "\n", normalized)
    normalized = re.sub(r"\s*[；;]\s*", "\n", normalized)
    items = [item.strip(" ；;。") for item in re.split(r"\n+|(?<![A-Za-z0-9])-", normalized) if item.strip(" ；;。")]
    items = [re.sub(r"^\s*(?:[○•●◦·。．.]|[oO](?=\s+))\s*", "", item).strip() for item in items]
    items = [re.sub(r"^(?:判断|结论)\s*[:：]\s*", "", item).strip() for item in items]
    items = [item for item in items if item]
    if len(items) >= 2:
        return "<ul>" + "".join(f"<li>{_highlight_ai_text(item)}</li>" for item in items[:4]) + "</ul>"
    if items:
        return f"<span>{_highlight_ai_text(items[0])}</span>"
    return f"<span>{_highlight_ai_text(text)}</span>"


def _format_team_ai_answer(parsed: list[tuple[str, str, str, str]]) -> str:
    ordered = [item for item in parsed if item[0] in TEAM_AI_KEYS]
    if not ordered:
        return ""
    cards = []
    for key, label, color, value in ordered:
        main_cls = " helixharness-ai-team-main" if key == "conclusion" else ""
        cards.append(
            f'<div class="helixharness-ai-team-card helixharness-ai-team-{key}{main_cls}" style="--ai-color:{color};">'
            f'<div class="helixharness-ai-team-card-head"><span>{_esc(label)}</span><i></i></div>'
            f'<div class="helixharness-ai-team-card-body">{_format_ai_section_body(value)}</div>'
            f'</div>'
        )
    return f'<div class="helixharness-ai-team-grid">{"".join(cards)}</div>'


def _highlight_ai_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    keyword_styles = {
        "risk": (
            "严重不足|极度异常|低有效产出|未形成稳定提效|卡点|不足|失败|风险|返工|问题|"
            "爆发|不一致|未生效|脱节|偏离|反复|待澄清|无法|没有|未闭环|未采集"
        ),
        "warn": (
            "高资源消耗|高介入|人工干预|质量门禁|前置质量|需求澄清|上下文膨胀|"
            "消耗路径|预算阈值|投入产出比|ROI|review|findings|major|clarification"
        ),
        "good": "已完成|通过|闭环|跑通|completed|archive成功|采集可靠|成功",
    }
    token_re = (
        r"\d+(?:\.\d+)?\s*(?:万|k|K|%|tokens?|Token|行|条|次|个|轮|阶段|用户|项目|特性|需求)?"
        r"|1:\d+"
    )
    pattern = re.compile(
        f"({token_re}|{keyword_styles['risk']}|{keyword_styles['warn']}|{keyword_styles['good']})",
        re.IGNORECASE,
    )
    parts = []
    last = 0
    for match in pattern.finditer(raw):
        start, end = match.span()
        if start > last:
            parts.append(_esc(raw[last:start]))
        token = match.group(0)
        lower = token.lower()
        cls = "num"
        if re.search(keyword_styles["risk"], token, re.IGNORECASE):
            cls = "risk"
        elif re.search(keyword_styles["good"], token, re.IGNORECASE):
            cls = "good"
        elif re.search(keyword_styles["warn"], token, re.IGNORECASE):
            cls = "warn"
        elif lower in {"", "-"}:
            cls = ""
        if cls:
            parts.append(f'<mark class="helixharness-ai-mark {cls}">{_esc(token)}</mark>')
        else:
            parts.append(_esc(token))
        last = end
    if last < len(raw):
        parts.append(_esc(raw[last:]))
    return "".join(parts)


def _format_ai_answer(text: str, variant: str = "") -> str:
    parsed = _parse_ai_answer_sections(text)
    if variant == "team" and parsed:
        team_html = _format_team_ai_answer(parsed)
        if team_html:
            return team_html
    if parsed:
        cards = "".join(
            f'<div class="helixharness-ai-result-card helixharness-ai-result-{key}" style="--ai-color:{color};">'
            f'<div class="helixharness-ai-result-label"><i></i><b>{_esc(label)}</b></div>'
            f'<div class="helixharness-ai-result-body">{_format_ai_section_body(value)}</div></div>'
            for key, label, color, value in parsed
        )
        return f'<div class="helixharness-ai-result-grid">{cards}</div>'
    lines = [line.strip() for line in str(text or "").splitlines()]
    blocks = []
    for line in lines:
        if not line:
            continue
        if re.match(r"^\d+[.、]", line):
            blocks.append(f'<li>{_esc(line)}</li>')
        else:
            blocks.append(f'<p>{_esc(line)}</p>')
    if not blocks:
        return '<p>模型 API 未返回可展示的分析内容。</p>'
    if any(block.startswith("<li>") for block in blocks):
        return "<ul>" + "".join(block if block.startswith("<li>") else f"<li>{block[3:-4]}</li>" for block in blocks) + "</ul>"
    return "".join(blocks)


def _ai_generated_html(title: str, scope: str, insight: dict) -> str:
    status = insight.get("status")
    insight_scope = str(insight.get("scope") or "")
    generated_at = str(insight.get("generated_at") or "")[:19].replace("T", " ")
    stats = insight.get("stats") if isinstance(insight.get("stats"), dict) else {}
    meta = []
    if generated_at:
        meta.append(f"生成于 {generated_at}")
    if stats.get("total_tokens") is not None:
        meta.append(f"Token {_fmt_tokens(stats.get('total_tokens'))}")
    if stats.get("total_elapsed") is not None:
        meta.append(f"耗时 {_fmt_duration(stats.get('total_elapsed'))}")
    if status == "success":
        answer_text = str(insight.get("answer", "") or "")
        retired_terms = (
            "质量信号", "质量风险", "质量评分", "质量分", "成熟度", "熟练度", "辅助分", "采用状态",
            "业务归档", "工作流完整", "统计收口", "验收事实", "返工/修正",
            "流程状态", "验收结果", "调整事件",
        )
        if any(term in answer_text for term in retired_terms):
            return ""
        # 检测 prompt 回显（模型 API 服务返回了 prompt 模板而非分析结果）
        if any(kw in answer_text[:200] for kw in ("分析请求", "角色：", "约束条件", "输出格式", "请根据以下")):
            return ""
        return (
            f'<div class="helixharness-panel helixharness-ai-panel helixharness-ai-generated helixharness-ai-scope-{_esc(insight_scope or "default")}">'
            f'<div class="helixharness-ai-head"><div><div class="helixharness-section-title">{_esc(title)}</div>'
            f'<p>{_esc(scope)}</p></div><span>模型 API 已生成</span></div>'
            f'<div class="helixharness-ai-answer">{_format_ai_answer(answer_text, insight_scope)}</div>'
            f'<div class="helixharness-ai-meta">{_esc(" · ".join(meta))}</div>'
            f'</div>'
        )
    if status == "error":
        return ""
    return ""


def _team_ai_placeholder_html(summary: dict, features: list[dict], users: list[dict], ai_insights: dict | None = None) -> str:
    team_insight = (ai_insights or {}).get("team") if isinstance((ai_insights or {}).get("team"), dict) else None
    if team_insight:
        html = _ai_generated_html(
            "全局 AI 解读",
            "结合全部项目统计、当天观测变化与历史摘要，解读归档、耗时、资源和可确认异常。",
            team_insight,
        )
        if html:
            return html
    return _ai_placeholder_html(
        "全局 AI 解读",
        "基于全部项目统计、当天观测变化与历史摘要生成，不评价用户。",
        notice=_ai_missing_notice(ai_insights, "全局 AI 解读"),
        stale=(ai_insights or {}).get("status") == "stale",
    )


def _feature_ai_entry_html(f: dict, ai_detail_id: str) -> str:
    usage = f.get("resource_usage") or {}
    evidence_bits = [
        f"流程 {_completed_stage_count(f)}/{int(f.get('expected_stage_count') or len(STAGE_ORDER))}",
        f"Token {_fmt_tokens(usage.get('total_tokens'))}",
        f"耗时 {_fmt_duration(f.get('total_duration_sec'))}",
    ]
    return (
        '<div class="helixharness-panel helixharness-ai-entry-panel">'
        '<div class="helixharness-ai-entry-copy">'
        '<div class="helixharness-section-title">AI 深度解读</div>'
        '<p>只解读当前项目的关键事实、可确认卡点和建议动作。</p>'
        f'<span>{_esc(" · ".join(evidence_bits))}</span>'
        '</div>'
        f'<a class="helixharness-action helixharness-ai-entry-action" href="#{ai_detail_id}">查看 AI 深度解读</a>'
        '</div>'
    )


def _feature_ai_detail_page(f: dict, ai_detail_id: str, detail_id: str, ai_insights: dict | None = None) -> str:
    usage = f.get("resource_usage") or {}
    metrics = "".join([
        _mini_metric(
            _progress_text(f),
            "流程进度 / 最近阶段",
            "funnel",
            hint=_progress_hint(f),
        ),
        _mini_metric(_fmt_duration(f.get("total_duration_sec")), "阶段累计耗时", "duration", hint=_stage_duration_summary(f)),
        _mini_metric(_fmt_tokens(usage.get("total_tokens")), "Token", "resource_usage", hint=_resource_summary_line(usage)),
    ])
    return (
        f'<section id="{ai_detail_id}" class="helixharness-detail-page helixharness-ai-detail-page">'
        f'<div class="helixharness-detail-page-head"><div><b>{_esc(f["feature"])}</b><span>AI 深度解读 · {_esc(f["project_name"])}</span></div><div class="helixharness-detail-actions"><a href="#{detail_id}">返回需求详情</a><a href="#helixharness-usage-top">返回总览</a></div></div>'
        '<div class="helixharness-ai-detail-intro">'
        '<b>解读范围</b><span>只解释当前项目的流程、耗时、Token、代码变化和可确认异常；不评价参与用户，异步数据未关联时不按 0 推断。</span>'
        '</div>'
        f'<div class="helixharness-mini-row helixharness-detail-metrics">{metrics}</div>'
        f'{_feature_ai_placeholder_html(f, ai_insights)}'
        '</section>'
    )


def _feature_ai_placeholder_html(f: dict, ai_insights: dict | None = None) -> str:
    key = f'{f.get("project_id")}::{f.get("feature")}'
    feature_insights = (ai_insights or {}).get("features") if isinstance((ai_insights or {}).get("features"), dict) else {}
    insight = feature_insights.get(key) or feature_insights.get(str(f.get("feature")))
    if isinstance(insight, dict):
        html = _ai_generated_html(
            "AI 项目复盘",
            f'模型 API 基于 {f.get("feature", "项目")} 的流程事实和已关联异步证据生成。',
            insight,
        )
        if html:
            return html
    return _ai_placeholder_html(
        "AI 项目复盘",
        f'基于 {_esc(f.get("feature", "项目"))} 的阶段、耗时、Token、代码变化和明确异常生成。',
        notice=_ai_missing_notice(ai_insights, "AI 项目复盘"),
        stale=(ai_insights or {}).get("status") == "stale",
    )


def render_usage_global_overview_html(
    roots: list[str],
    rejected_paths: set[str] | None = None,
    linked_supplements: list[LinkedStageSupplement] | None = None,
    *, runtime_period: str = "all", runtime_start: str = "", runtime_end: str = "", runtime_expanded: bool = False,
    view: str = "project", recent_days: int = 1, first_seen: dict | None = None,
    dashboard_data: UsageDashboardData | None = None,
) -> str:
    return render_usage_overview_html(
        roots, "__global_overview__", rejected_paths, linked_supplements,
        runtime_period=runtime_period, runtime_start=runtime_start, runtime_end=runtime_end, runtime_expanded=runtime_expanded,
        view=view, recent_days=recent_days, first_seen=first_seen, dashboard_data=dashboard_data,
    )


def render_usage_overview_html(
    roots: list[str],
    view_mode: str = "按用户维度",
    rejected_paths: set[str] | None = None,
    linked_supplements: list[LinkedStageSupplement] | None = None,
    *, runtime_period: str = "all", runtime_start: str = "", runtime_end: str = "", runtime_expanded: bool = False,
    view: str = "project", recent_days: int = 1, first_seen: dict | None = None,
    dashboard_data: UsageDashboardData | None = None,
) -> str:
    if not roots:
        return (
            '<div class="helixharness-empty helixharness-empty-guide">'
            '<div class="helixharness-empty-title">暂无统计数据</div>'
            '<div class="helixharness-empty-desc">尚未同步到任何仓库的 eval 遥测数据。</div>'
            '<div class="helixharness-empty-steps">'
            '<span class="helixharness-step-num">1</span><span>在「配置」页添加项目仓库</span>'
            '<span class="helixharness-step-num">2</span><span>点击「立即同步」拉取代码</span>'
            '<span class="helixharness-step-num">3</span><span>仓库 <code>docs/eval/*/eval-log.json</code> 会自动汇总到这里</span>'
            '</div></div>'
        )
    dashboard_data = dashboard_data or load_usage_dashboard_data(roots, rejected_paths, linked_supplements)
    all_agg = dashboard_data.aggregate
    agg = all_agg
    s = agg["summary"]
    if all_agg["summary"]["feature_total"] == 0 and not all_agg["summary"].get("invalid_log_count"):
        return (
            '<div class="helixharness-empty">已同步 ' + str(len(roots)) + ' 个仓库，但均未发现 '
            '<code>docs/eval/*/eval-log.json</code>。<br>请确认统计文件已提交到仓库分支，或等待下次同步。</div>'
        )

    features = agg["features"]
    installations = _summarize_user_rows(agg["user_project"])
    from eval.usage_ai import usage_data_state_hash

    ai_insights = _load_ai_insights(roots, usage_data_state_hash(all_agg))
    mode_key = "user" if "用户" in (view_mode or "") else "combined"
    feature_pages = ""
    installation_pages = ""
    active_table = active_hint = ""
    if view_mode == "__global_overview__":
        pass
    elif mode_key == "user":
        active_table, installation_pages = _combined_user_feature_table_html(installations, features, ai_insights)
        active_hint = "按用户归组；展开后查看该用户参与的项目，明细列与需求维度保持一致。"
    else:
        rows, feature_pages = _feature_table_rows(features, ai_insights, split_identity=True)
        active_table = _feature_table_html(rows, main=False, split_identity=True)
        active_hint = "按日志时间列出各仓库的需求，查看代码库、需求名和开发人。"
    active_hint += " 日志时间均为北京时间。"
    total = int(s.get("feature_total", 0) or 0)
    installation_count = len(installations)
    archived = int(s.get("archived", 0) or 0)
    average_duration, _, _ = _archived_duration_average(features)
    resource_usage = s.get("resource_usage") or {}
    cards = "".join([
        _card(total, "项目数", _ACCENT, tip_key="feature_total"),
        _card(installation_count, "用户数", _PURPLE, tip_key="user_total"),
        _card(_fmt_tokens(resource_usage.get("total_tokens")), "Token", _CYAN, tip_key="token_summary"),
        _card(
            _fmt_duration_hours(average_duration),
            "平均耗时",
            _BLUE,
            tip_key="duration_summary",
        ),
        _card(f"{archived}/{total}", "归档完成", _GREEN, "已归档 / 项目总数", "archived"),
    ])
    recent_days = recent_days if recent_days in {1, 3, 7, 30} else 1
    overview_controls = (
        '<form id="helixharness-overview-form" action="/" method="get" class="helixharness-overview-controls" aria-label="概览统计条件">'
        f'<input type="hidden" name="view" value="{_esc(view)}">'
        f'<input type="hidden" name="runtime_period" value="{_esc(runtime_period)}">'
        f'<input type="hidden" name="runtime_start" value="{_esc(runtime_start)}">'
        f'<input type="hidden" name="runtime_end" value="{_esc(runtime_end)}">'
        f'<input type="hidden" name="recent_days" value="{recent_days}">'
        '</form>'
    )
    overview_panel = (
        '<div class="helixharness-panel helixharness-overview-panel">'
        '<div class="helixharness-section-head"><div><div class="helixharness-section-title">整体概览</div>'
        '<div class="helixharness-section-hint">汇总项目、用户及执行情况；下方可查看需求明细。</div></div>'
        f'{overview_controls}</div>'
        f'<div class="helixharness-grid">{cards}</div>'
        f'{_recent_growth_html(features, recent_days, first_seen)}'
        '</div>'
        f'{_runtime_overview_html(all_agg["features"], runtime_period, runtime_start, runtime_end, expanded=runtime_expanded, view=view, recent_days=recent_days)}'
    )
    style = f"""
<style>
.helixharness-usage-root {{ --helixharness-ink:{_TEXT}; --helixharness-muted:{_MUTED}; --helixharness-border:{_BORDER}; --helixharness-accent:{_ACCENT}; color:var(--helixharness-ink); font-family:"PingFang SC","Microsoft YaHei","Helvetica Neue",Arial,sans-serif; width:100%; }}
.helixharness-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin:0 0 14px; }}
.helixharness-metric {{ position:relative; overflow:visible; background:#fff; border:1px solid var(--helixharness-border); border-radius:6px; padding:12px 14px; min-height:86px; box-shadow:0 1px 2px rgba(29,33,41,.04); }}
.helixharness-metric:has(.helixharness-tip-wrap:hover),.helixharness-metric:has(.helixharness-tip-wrap:focus-within) {{ z-index:10030; }}
.helixharness-metric::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--metric); }}
.helixharness-metric-head {{ display:flex; align-items:center; justify-content:space-between; gap:8px; color:var(--helixharness-muted); font-size:12px; font-weight:600; }}
.helixharness-metric-head i {{ width:20px; height:2px; border-radius:999px; background:var(--metric); opacity:.75; }}
.helixharness-metric-num {{ margin-top:11px; color:var(--helixharness-ink); font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:28px; font-weight:800; line-height:1; }}
.helixharness-metric-sub {{ margin-top:8px; color:var(--helixharness-muted); font-size:11.5px; line-height:1.3; }}
.helixharness-panel {{ background:#fff; border:1px solid var(--helixharness-border); border-radius:6px; padding:14px; margin:0 0 14px; box-shadow:0 1px 2px rgba(29,33,41,.04); overflow:visible; }}
.helixharness-duration-share > p {{ margin:5px 0 12px; color:var(--helixharness-muted); font-size:11.5px; }}
.helixharness-duration-ring-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(146px,1fr)); gap:12px; margin-top:14px; }}
.helixharness-duration-item {{ display:flex; flex-direction:column; align-items:center; min-width:0; padding:14px 10px 12px; border:1px solid #E0E8F2; border-radius:6px; background:linear-gradient(180deg,#FFFFFF 0%,#FAFCFF 100%); }}
.helixharness-duration-ring {{ --share:0%; --ring:#1677FF; position:relative; display:grid; place-items:center; width:126px; height:126px; border-radius:50%; background:conic-gradient(var(--ring) var(--share),#EDF1F6 0); box-shadow:0 4px 14px rgba(29,78,137,.08); }}
.helixharness-duration-ring::after {{ content:""; position:absolute; inset:12px; border-radius:50%; background:#fff; box-shadow:inset 0 0 0 1px rgba(217,226,237,.7); }}
.helixharness-duration-ring-core {{ position:relative; z-index:1; display:grid; gap:5px; text-align:center; }}
.helixharness-duration-ring-core strong {{ color:var(--ring); font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:23px; font-weight:800; line-height:1; }}
.helixharness-duration-ring-core span {{ color:var(--helixharness-muted); font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:11.5px; font-weight:600; }}
.helixharness-duration-item > b {{ margin-top:10px; color:var(--helixharness-ink); font-size:12.5px; }}
.helixharness-duration-item > small {{ margin-top:3px; color:var(--helixharness-muted); font-size:10.5px; }}
.helixharness-overview-distributions {{ margin-bottom:14px; }}
.helixharness-runtime-disclosure {{ border:1px solid #D9E3F0; border-radius:6px; background:#FFF; }}
.helixharness-runtime-disclosure > summary {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding:12px 14px; list-style:none; cursor:pointer; font-size:12px; }}
.helixharness-runtime-disclosure > summary::-webkit-details-marker {{ display:none; }}
.helixharness-runtime-toggle {{ color:#1677FF; font-weight:600; }}
.helixharness-runtime-toggle::before {{ content:attr(data-open) " ＋"; }}
.helixharness-runtime-disclosure[open] .helixharness-runtime-toggle::before {{ content:attr(data-close) " −"; }}
.helixharness-runtime-disclosure-body {{ padding:0 14px 14px; }}
.helixharness-runtime-panels {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:12px; }}
.helixharness-runtime-panels > .helixharness-panel {{ min-width:0; margin:0; box-shadow:none; }}
.helixharness-runtime-filter {{ margin:5px 0 12px; display:flex; align-items:center; flex-wrap:wrap; gap:8px; font-size:11.5px; color:var(--helixharness-muted); }}
.helixharness-runtime-filter nav,.helixharness-runtime-filter form {{ display:flex; align-items:center; flex-wrap:wrap; gap:6px; margin:0; }}
.helixharness-runtime-filter a,.helixharness-runtime-filter button,.helixharness-runtime-filter input {{ border:1px solid #D9E3F0; background:#FFF; border-radius:5px; padding:6px 9px; color:var(--helixharness-ink); font:inherit; text-decoration:none; }}
.helixharness-runtime-filter a[aria-current],.helixharness-runtime-filter button {{ background:#E8F3FF; border-color:#91CAFF; color:#0958D9; cursor:pointer; }}
.helixharness-runtime-filter input {{ min-width:0; max-width:140px; }}
.helixharness-runtime-range-hint {{ color:var(--helixharness-muted); }}
.helixharness-runtime-range-error {{ color:#CF1322; }}
.helixharness-runtime-distribution > p {{ margin:5px 0 12px; color:var(--helixharness-muted); font-size:11.5px; }}
.helixharness-version-bars {{ display:grid; gap:2px; margin:12px 0 0; padding:0; list-style:none; }}
.helixharness-version-bars a {{ display:grid; grid-template-columns:minmax(90px,1fr) minmax(60px,2fr) 110px; align-items:center; gap:12px; padding:9px 6px; border-radius:5px; color:var(--helixharness-ink); text-decoration:none; font-size:12px; }}
.helixharness-version-bars a:hover,.helixharness-version-bars a:focus-visible {{ background:#F0F7FF; outline:2px solid #91CAFF; outline-offset:-2px; }}
.helixharness-version-label {{ overflow-wrap:anywhere; font-variant-numeric:tabular-nums; line-height:1.4; }}
.helixharness-version-track {{ height:14px; border-radius:3px; background:#F0F3F8; overflow:hidden; }}
.helixharness-version-track i {{ display:block; height:100%; min-width:2px; border-radius:3px; background:#1677FF; }}
.helixharness-version-value {{ display:flex; justify-content:space-between; align-items:baseline; gap:8px; white-space:nowrap; font-variant-numeric:tabular-nums; }}
.helixharness-version-value b {{ font-weight:600; }}
.helixharness-version-value small {{ color:var(--helixharness-muted); font-size:11px; }}
@media (max-width:540px) {{ .helixharness-version-bars a {{ grid-template-columns:minmax(0,1fr) 110px; gap:5px 12px; padding:10px 4px; }} .helixharness-version-label {{ grid-column:1; grid-row:1; }} .helixharness-version-value {{ grid-column:2; grid-row:1; }} .helixharness-version-track {{ grid-column:1 / -1; grid-row:2; height:12px; }} }}
.helixharness-runtime-chart {{ display:flex; align-items:center; gap:24px; max-width:620px; margin:16px auto; }}
.helixharness-runtime-ring {{ flex-shrink:0; background:none; }}
.helixharness-runtime-ring::after {{ display:none; }}
.helixharness-runtime-ring svg {{ position:absolute; inset:0; width:100%; height:100%; }}
.helixharness-runtime-ring .helixharness-duration-ring-core {{ pointer-events:none; }}
.helixharness-runtime-ring circle {{ pointer-events:stroke; }}
.helixharness-runtime-ring a:hover circle,.helixharness-runtime-ring a:focus circle {{ opacity:.7; }}
.helixharness-runtime-legend {{ flex:1; min-width:0; margin:0; padding:0; list-style:none; max-height:210px; overflow-y:auto; }}
.helixharness-runtime-legend a {{ display:grid; grid-template-columns:8px minmax(0,1fr) auto auto; align-items:center; gap:8px; padding:8px 3px; color:var(--helixharness-ink); text-decoration:none; font-size:12px; }}
.helixharness-runtime-legend li {{ border-bottom:1px solid #F0F3F8; }}
.helixharness-runtime-legend a:hover,.helixharness-runtime-legend a:focus-visible {{ background:#E8F3FF; outline:2px solid #91CAFF; outline-offset:-2px; }}
.helixharness-runtime-legend i {{ width:8px; height:8px; border-radius:50%; }}
.helixharness-runtime-legend span {{ overflow-wrap:anywhere; }}
.helixharness-runtime-legend small {{ color:var(--helixharness-muted); white-space:nowrap; }}
.helixharness-runtime-legend b {{ white-space:nowrap; font-variant-numeric:tabular-nums; }}
@media (max-width:540px) {{ .helixharness-runtime-chart {{ flex-direction:column; gap:12px; }} .helixharness-runtime-legend {{ width:100%; }} }}
.helixharness-log-time {{ color:var(--helixharness-muted); font-size:11.5px; white-space:nowrap; font-variant-numeric:tabular-nums; }}
.helixharness-log-time-cell .helixharness-log-clock {{ display:block; margin-top:2px; }}
@media (max-width:900px) {{ .helixharness-runtime-panels {{ grid-template-columns:minmax(0,1fr); }} .helixharness-usage-root .helixharness-requirement-group > summary {{ grid-template-columns:minmax(0,1fr) auto; }} .helixharness-requirement-counts {{ white-space:normal !important; }} }}
.helixharness-rules-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px; margin-top:10px; }}
.helixharness-rule-item {{ display:grid; gap:4px; padding:10px 11px; border:1px solid #D9E8FF; border-radius:6px; background:#F8FBFF; }}
.helixharness-rule-item b {{ color:#0958D9; font-size:12px; }}
.helixharness-rule-item span {{ color:var(--helixharness-muted); font-size:11.5px; line-height:1.5; }}
.helixharness-recent-growth {{ margin-top:16px; padding-top:14px; border-top:1px solid #E5EAF1; }}
.helixharness-recent-heading {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; font-size:12px; }}
.helixharness-recent-heading > span,.helixharness-recent-growth > p {{ color:#86909C; font-size:11px; }}
.helixharness-recent-heading nav {{ display:flex; gap:4px; }}
.helixharness-recent-heading button {{ padding:4px 9px; border:1px solid #D9E3F0; border-radius:5px; background:#fff; color:#4E5969; cursor:pointer; font:inherit; }}
.helixharness-recent-heading button[aria-current="true"] {{ color:#0958D9; border-color:#91CAFF; background:#E8F3FF; }}
.helixharness-recent-numbers {{ display:flex; gap:30px; align-items:baseline; flex-wrap:wrap; margin:12px 0 6px; font-size:13px; }}
.helixharness-recent-numbers strong {{ color:#1677FF; font:700 23px "SFMono-Regular",Consolas,monospace; margin:0 5px; }}
.helixharness-recent-growth > p {{ margin:0; line-height:1.5; }}
.helixharness-overview-controls {{ display:none; }}
@media (max-width:640px) {{ .helixharness-overview-panel .helixharness-section-head {{ flex-wrap:wrap; }} }}
.helixharness-overview-panel .helixharness-grid {{ margin:0; }}
.helixharness-overview-panel {{ padding:16px; border-color:#DCE7F5; box-shadow:0 10px 30px rgba(22,33,62,.07); }}
.helixharness-overview-panel .helixharness-metric {{ min-height:108px; padding:15px 16px; border-color:#E0E8F2; background:linear-gradient(145deg,#FFFFFF 0%,#F7FAFE 100%); box-shadow:0 8px 22px rgba(29,33,41,.07); transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease; }}
.helixharness-overview-panel .helixharness-metric:hover {{ transform:translateY(-2px); border-color:#B7D8FF; box-shadow:0 14px 34px rgba(22,119,255,.12); }}
.helixharness-overview-panel .helixharness-metric::before {{ width:5px; border-radius:6px 0 0 6px; }}
.helixharness-overview-panel .helixharness-metric-head {{ font-size:15px; color:#17233D; font-weight:800; letter-spacing:.01em; }}
.helixharness-overview-panel .helixharness-metric-head i {{ width:28px; height:4px; }}
.helixharness-overview-panel .helixharness-metric-num {{ color:var(--metric); font-size:36px; letter-spacing:0; }}
.helixharness-overview-panel .helixharness-metric-num.raw {{ margin-top:12px; display:flex; align-items:center; flex-wrap:wrap; gap:6px; color:var(--helixharness-ink); font-family:inherit; font-size:12px; line-height:1.4; }}
.helixharness-overview-panel .helixharness-metric-num.raw .helixharness-focus-tag {{ min-height:26px; padding:0 9px; font-size:11.5px; box-shadow:0 4px 12px rgba(29,33,41,.06); }}
.helixharness-overview-panel .helixharness-metric-sub {{ font-size:12px; color:#4E5969; min-height:16px; }}
.helixharness-metric-pair {{ display:grid; grid-template-columns:1fr; margin-top:8px; }}
.helixharness-metric-pair > div {{ display:grid; grid-template-columns:52px minmax(0,1fr); gap:8px; align-items:center; min-width:0; padding:8px 0; }}
.helixharness-metric-pair > div:first-child {{ border-bottom:1px solid #DCE5F0; }}
.helixharness-pair-label {{ display:block; color:var(--helixharness-muted); font-size:11px; font-weight:600; line-height:1; }}
.helixharness-pair-value {{ display:block; min-width:0; color:var(--metric); font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:21px; font-weight:800; line-height:1.05; text-align:right; white-space:nowrap; }}
.helixharness-section-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin:0 0 12px; padding-bottom:10px; border-bottom:1px solid #F0F0F0; }}
.helixharness-section-title {{ color:var(--helixharness-ink); font-size:14px; font-weight:700; }}
.helixharness-section-hint {{ color:var(--helixharness-muted); font-size:12px; line-height:1.5; }}
.helixharness-dimension-panel {{ position:relative; padding:16px; border-color:#BFD7FF; background:linear-gradient(180deg,#FFFFFF 0%,#FAFCFF 100%); box-shadow:0 12px 34px rgba(22,119,255,.09); }}
.helixharness-dimension-panel::before {{ content:""; position:absolute; left:0; top:0; right:0; height:3px; border-radius:6px 6px 0 0; background:linear-gradient(90deg,#1677FF,#36CFC9); }}
.helixharness-dimension-head {{ margin-bottom:14px; border-bottom-color:#D9E8FF; }}
.helixharness-dimension-panel .helixharness-section-title {{ display:flex; align-items:center; gap:9px; color:#0958D9; font-size:16px; }}
.helixharness-dimension-panel .helixharness-table-wrap {{ padding:10px; border-color:#BFD7FF; background:#F7FBFF; box-shadow:inset 0 1px 0 rgba(255,255,255,.7),0 8px 22px rgba(22,119,255,.08); }}
.helixharness-dimension-panel .helixharness-table {{ border:1px solid #D9E8FF; border-radius:6px; overflow:hidden; }}
.helixharness-dimension-panel .helixharness-table th {{ background:#EEF6FF; color:#1D4ED8; border-bottom-color:#BFD7FF; }}
.helixharness-dimension-panel .helixharness-table td {{ background:#FFFFFF; }}
.helixharness-dimension-panel .helixharness-main-table tbody tr:hover td {{ background:#F3F9FF; }}
.helixharness-dimension-panel .helixharness-num {{ font-size:15px; color:#0958D9; }}
.helixharness-table-wrap {{ width:100%; overflow:visible; border:1px solid #E5E6EB; border-radius:6px; }}
.helixharness-table {{ width:100%; border-collapse:separate; border-spacing:0; font-size:12.5px; background:#fff; }}
.helixharness-table th {{ position:sticky; top:0; z-index:1; text-align:left; padding:10px 11px; color:var(--helixharness-muted); background:#F5F7FA; border-bottom:1px solid var(--helixharness-border); font-size:12px; font-weight:700; white-space:nowrap; }}
.helixharness-table th:has(.helixharness-tip-wrap:hover),.helixharness-table th:has(.helixharness-tip-wrap:focus-within) {{ z-index:10040; }}
.helixharness-table td {{ padding:11px; border-bottom:1px solid #F0F0F0; vertical-align:middle; color:var(--helixharness-ink); white-space:nowrap; }}
.helixharness-main-table {{ table-layout:fixed; width:100%; }}
.helixharness-main-table th,.helixharness-main-table td {{ white-space:normal; overflow-wrap:anywhere; }}
.helixharness-requirement-child-table {{ table-layout:fixed; width:100%; min-width:1040px; }}
.helixharness-requirement-child-table .helixharness-col-repository {{ width:11%; }}
.helixharness-requirement-child-table .helixharness-col-requirement {{ width:14%; }}
.helixharness-requirement-child-table .helixharness-col-developer {{ width:10%; }}
.helixharness-requirement-child-table .helixharness-col-log-time {{ width:12%; }}
.helixharness-requirement-child-table .helixharness-col-progress {{ width:15%; }}
.helixharness-requirement-child-table .helixharness-col-intervention {{ width:11%; }}
.helixharness-requirement-child-table .helixharness-col-duration {{ width:8%; }}
.helixharness-requirement-child-table .helixharness-col-token {{ width:12%; }}
.helixharness-requirement-child-table .helixharness-col-action {{ width:7%; }}
.helixharness-requirement-child-table th,.helixharness-requirement-child-table td {{ padding:13px 12px; white-space:normal; overflow-wrap:break-word; word-break:normal; line-height:1.5; }}
.helixharness-requirement-child-table td:nth-child(7),.helixharness-requirement-child-table td:nth-child(8),.helixharness-requirement-child-table td:nth-child(9) {{ white-space:nowrap; }}
.helixharness-table tr:last-child td {{ border-bottom:0; }}
.helixharness-table td span {{ color:var(--helixharness-muted); font-size:11.5px; }}
.helixharness-main-table tbody tr:hover {{ background:#F7FBFF; }}
.helixharness-requirement-row > td {{ padding:8px !important; background:#F7FBFF !important; }}
.helixharness-requirement-group {{ border:1px solid #BFD7FF; border-radius:6px; background:#FFFFFF; overflow:visible; }}
.helixharness-requirement-group > summary {{ display:grid; grid-template-columns:minmax(0,1fr) auto auto auto; align-items:center; gap:14px; padding:12px 14px; cursor:pointer; list-style:none; }}
.helixharness-requirement-group > summary::-webkit-details-marker {{ display:none; }}
.helixharness-requirement-identity {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px; min-width:0; }}
.helixharness-requirement-label {{ display:inline-flex; align-items:center; min-height:22px; padding:0 9px; border-radius:999px; color:#4E5969 !important; background:#F2F3F5; border:1px solid #D9DDE5; font-size:11px !important; font-weight:700; }}
.helixharness-user-identity b {{ color:#17233D; font-size:14px; font-weight:800; }}
.helixharness-user-label {{ color:#5B2ABF !important; background:#F4EEFF; border-color:#D9C6FF; }}
.helixharness-user-id {{ color:#86909C; background:#F5F7FA; border:1px solid #E5E6EB; border-radius:4px; padding:2px 6px; font-size:10.5px; }}
.helixharness-requirement-counts {{ color:#4E5969 !important; font-size:11.5px !important; white-space:nowrap; }}
.helixharness-requirement-toggle {{ color:#1677FF !important; font-size:11.5px !important; font-weight:700; white-space:nowrap; }}
.helixharness-requirement-toggle::before {{ content:attr(data-open); }}
.helixharness-requirement-group[open] .helixharness-requirement-toggle::before {{ content:attr(data-close); }}
.helixharness-requirement-body {{ padding:0 10px 10px; }}
.helixharness-requirement-body > .helixharness-table-wrap {{ padding:0; overflow-x:auto !important; background:#FFFFFF; box-shadow:none; }}
.helixharness-num {{ color:#0958D9; font-weight:800; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; }}
.helixharness-action {{ display:inline-flex; align-items:center; justify-content:center; min-height:28px; padding:0 10px; border-radius:6px; color:#0958D9 !important; background:#E8F3FF; border:1px solid #91CAFF; font-size:12px; font-weight:700; text-decoration:none !important; }}
.helixharness-action:hover {{ background:#D6EFFF; }}
.helixharness-ai-entry-panel {{ display:flex; align-items:center; justify-content:space-between; gap:14px; padding:13px 14px; border-color:#C9DDF8; background:#F8FBFF; }}
.helixharness-ai-entry-copy {{ min-width:0; }}
.helixharness-ai-entry-copy p {{ margin:5px 0 4px; color:var(--helixharness-ink); font-size:13px; line-height:1.45; }}
.helixharness-ai-entry-copy span {{ display:block; color:var(--helixharness-muted); font-size:11.5px; line-height:1.45; overflow-wrap:anywhere; }}
.helixharness-ai-entry-action {{ flex:0 0 auto; min-height:34px; padding:0 14px; }}
.helixharness-badge {{ display:inline-flex; align-items:center; justify-content:center; min-height:18px; padding:2px 8px; border-radius:999px; font-size:10.5px; font-weight:600; white-space:nowrap; margin:1px 2px 1px 0; }}
.helixharness-status-stack {{ display:flex; flex-direction:column; align-items:flex-start; gap:4px; }}
.helixharness-pill,.helixharness-muted-pill {{ display:inline-flex; align-items:center; padding:2px 7px; border-radius:999px; font-size:10.5px; margin:1px 3px 1px 0; white-space:nowrap; font-weight:600; }}
.helixharness-muted-pill {{ background:#F2F3F5; color:#86909C; }}
.helixharness-focus-tags {{ display:flex; align-items:center; flex-wrap:wrap; gap:5px; min-width:180px; }}
.helixharness-focus-tag {{ display:inline-flex; align-items:center; min-height:22px; padding:0 8px; border-radius:999px; font-size:11px; font-weight:600; white-space:nowrap; }}
.helixharness-insight-title-row {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }}
.helixharness-insight-title-row p {{ margin:6px 0 0; color:var(--helixharness-muted); font-size:12px; }}
.helixharness-insight-title-row strong {{ flex:0 0 auto; display:inline-flex; align-items:center; min-height:24px; padding:0 10px; border-radius:999px; background:#FFF2F0; color:#CF1322; border:1px solid #FFCCC7; font-size:12px; }}
.helixharness-insight-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:10px; }}
.helixharness-insight {{ position:relative; display:grid; gap:6px; padding:12px 12px 12px 14px; border:1px solid #E5E6EB; border-radius:6px; background:#F7F9FC; overflow:hidden; }}
.helixharness-insight::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:4px; background:#91CAFF; }}
.helixharness-insight-critical {{ background:#FFF7F6; border-color:#FFCCC7; }}
.helixharness-insight-critical::before {{ background:#F5222D; }}
.helixharness-insight-warn {{ background:#FFFBE6; border-color:#FFE58F; }}
.helixharness-insight-warn::before {{ background:#FAAD14; }}
.helixharness-insight-ok {{ background:#F6FFED; border-color:#B7EB8F; }}
.helixharness-insight-ok::before {{ background:#52C41A; }}
.helixharness-insight-top {{ display:flex; align-items:center; justify-content:space-between; gap:8px; }}
.helixharness-insight-top i {{ flex:0 0 auto; display:inline-flex; align-items:center; min-height:18px; padding:0 7px; border-radius:999px; background:#fff; color:#86909C; border:1px solid #E5E6EB; font-size:10.5px; font-style:normal; font-weight:700; }}
.helixharness-insight-critical .helixharness-insight-top i {{ color:#CF1322; border-color:#FFCCC7; }}
.helixharness-insight-warn .helixharness-insight-top i {{ color:#AD6800; border-color:#FFE58F; }}
.helixharness-insight-ok .helixharness-insight-top i {{ color:#237804; border-color:#B7EB8F; }}
.helixharness-insight b {{ font-size:13px; color:var(--helixharness-ink); }}
.helixharness-insight span,.helixharness-insight em {{ color:var(--helixharness-muted); font-size:12px; line-height:1.45; font-style:normal; }}
.helixharness-insight em {{ color:#0958D9; }}
.helixharness-ai-panel {{ background:#FBFCFF; border-color:#BCD7FF; }}
.helixharness-ai-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:8px; }}
.helixharness-ai-head p {{ margin:4px 0 0; color:var(--helixharness-muted); font-size:12px; line-height:1.45; }}
.helixharness-ai-head span {{ flex:0 0 auto; display:inline-flex; align-items:center; min-height:22px; padding:0 8px; border-radius:999px; background:#E8F3FF; color:#0958D9; border:1px solid #91CAFF; font-size:11px; font-weight:700; }}
.helixharness-ai-empty {{ background:#FFFFFF; border-color:#D9E3F0; }}
.helixharness-ai-empty .helixharness-ai-head {{ margin-bottom:12px; }}
.helixharness-ai-empty .helixharness-ai-head > span {{ background:#F2F3F5; border-color:#E5E6EB; color:#4E5969; }}
.helixharness-ai-empty-state {{ display:flex; align-items:center; justify-content:space-between; gap:16px; min-height:68px; padding:13px 14px; border:1px dashed #B7D8FF; border-radius:6px; background:#F7FAFF; }}
.helixharness-ai-empty-state > div {{ display:grid; gap:4px; }}
.helixharness-ai-empty-state b {{ color:var(--helixharness-ink); font-size:14px; }}
.helixharness-ai-empty-state span {{ color:var(--helixharness-muted); font-size:12px; line-height:1.45; }}
.helixharness-ai-empty-state .helixharness-action {{ flex:0 0 auto; }}
.helixharness-ai-emphasis {{ margin:8px 0 10px; padding:8px 10px; border-left:3px solid #1677FF; background:#F0F7FF; color:#0958D9; border-radius:4px; font-size:12px; font-weight:700; line-height:1.45; }}
.helixharness-ai-empty-note {{ margin:8px 0 10px; padding:8px 10px; border:1px solid #FFE1A6; border-left:3px solid #FAAD14; background:#FFFBEF; color:#874D00; border-radius:4px; font-size:12px; font-weight:650; line-height:1.45; }}
.helixharness-ai-axis-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:8px; margin:10px 0 8px; }}
.helixharness-ai-axis {{ display:grid; gap:3px; min-height:78px; padding:10px; border:1px solid #D9E8FF; border-radius:6px; background:#fff; }}
.helixharness-ai-axis b {{ color:var(--helixharness-ink); font-size:12.5px; }}
.helixharness-ai-axis span {{ color:#0958D9; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:13px; font-weight:800; }}
.helixharness-ai-axis em {{ color:var(--helixharness-muted); font-size:11.5px; line-height:1.35; font-style:normal; }}
.helixharness-ai-panel ul {{ margin:8px 0 0 17px; padding:0; color:var(--helixharness-muted); font-size:12px; line-height:1.65; }}
.helixharness-ai-panel li {{ padding-left:2px; }}
.helixharness-ai-generated {{ border-color:#D9E3F0; background:#FFFFFF; }}
.helixharness-ai-generated .helixharness-ai-head span {{ background:#F2F7FF; color:#0958D9; border-color:#B7D8FF; }}
.helixharness-ai-scope-team .helixharness-ai-head {{ margin-bottom:6px; }}
.helixharness-ai-scope-team .helixharness-ai-answer {{ margin-top:8px; }}
.helixharness-ai-team-grid {{ display:grid; grid-template-columns:1.08fr 1fr 1fr; gap:10px; }}
.helixharness-ai-team-card {{ position:relative; min-height:108px; padding:14px 15px 13px 17px; border:1px solid #C9DDF8; border-radius:6px; background:linear-gradient(145deg,#FFFFFF 0%,#F3F8FF 100%); box-shadow:0 8px 22px rgba(29,33,41,.065); overflow:hidden; }}
.helixharness-ai-team-card::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:4px; border-radius:6px 0 0 6px; background:var(--ai-color); opacity:.58; }}
.helixharness-ai-team-card-head {{ display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:10px; color:#17233D; font-size:14.5px; font-weight:800; }}
.helixharness-ai-team-card-head i {{ flex:0 0 auto; width:25px; height:3px; border-radius:999px; background:var(--ai-color); opacity:.52; }}
.helixharness-ai-team-card-body span {{ display:block; color:#1D2129; font-size:13.5px; font-weight:600; line-height:1.62; overflow-wrap:anywhere; }}
.helixharness-ai-team-card-body ul {{ margin:0; padding:0; list-style:none !important; list-style-type:none !important; display:grid; gap:8px; }}
.helixharness-ai-team-card-body li {{ position:relative; margin:0; padding:2px 0 2px 9px; border-left:2px solid color-mix(in srgb,var(--ai-color) 42%,#DCE7F5); color:#1D2129; font-size:13.5px; font-weight:400; line-height:1.58; overflow-wrap:anywhere; list-style:none !important; }}
.helixharness-ai-team-card-body li::marker {{ content:"" !important; font-size:0 !important; }}
.helixharness-ai-team-card-body li::before {{ content:none !important; display:none !important; }}
.helixharness-ai-team-main {{ grid-column:auto; }}
.helixharness-ai-team-main .helixharness-ai-team-card-body span {{ color:#17233D; font-size:13.5px; font-weight:750; line-height:1.62; }}
.helixharness-ai-scope-team .helixharness-ai-mark {{ background:transparent !important; color:inherit !important; padding:0 !important; border-radius:0 !important; font-weight:inherit !important; }}
.helixharness-ai-scope-team .helixharness-ai-meta {{ margin-top:7px; }}
.helixharness-ai-scope-feature .helixharness-ai-head {{ margin-bottom:10px; }}
.helixharness-ai-scope-feature .helixharness-ai-result-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
.helixharness-ai-scope-feature .helixharness-ai-result-card {{ min-height:auto; padding:13px 15px; border-color:#DDE5F0; background:#FFFFFF; }}
.helixharness-ai-scope-feature .helixharness-ai-result-card::before {{ width:4px; background:var(--ai-color); opacity:.72; }}
.helixharness-ai-scope-feature .helixharness-ai-result-conclusion {{ grid-column:1/-1; padding:15px 16px; background:#F8FAFD; border-color:#C9DDF8; }}
.helixharness-ai-scope-feature .helixharness-ai-result-conclusion .helixharness-ai-result-body span {{ font-size:15px; font-weight:700; line-height:1.55; color:#1D2129; }}
.helixharness-ai-scope-feature .helixharness-ai-result-proficiency {{ background:#FCFAFF; border-color:#E4D7F7; }}
.helixharness-ai-scope-feature .helixharness-ai-result-delivery_efficiency {{ background:#FFFCF2; border-color:#F3E2B5; }}
.helixharness-ai-scope-feature .helixharness-ai-result-evidence,.helixharness-ai-scope-feature .helixharness-ai-result-flow {{ background:#FFFFFF; }}
.helixharness-ai-scope-feature .helixharness-ai-result-flow {{ grid-column:1/-1; }}
.helixharness-ai-scope-feature .helixharness-ai-result-action {{ grid-column:1/-1; background:#FBFEFC; border-color:#D9EBD6; }}
.helixharness-ai-scope-feature .helixharness-ai-result-label {{ margin:-2px 0 10px; padding-bottom:8px; border-bottom:1px solid #EEF2F7; }}
.helixharness-ai-scope-feature .helixharness-ai-result-label b {{ font-size:14px; }}
.helixharness-ai-scope-feature .helixharness-ai-result-body ul {{ padding-left:0; list-style:none; display:grid; gap:8px; }}
.helixharness-ai-scope-feature .helixharness-ai-result-body li {{ position:relative; margin:0; padding:8px 10px 8px 24px; border:1px solid #EEF2F7; border-radius:6px; background:#F8FAFD; font-size:13px; line-height:1.55; }}
.helixharness-ai-scope-feature .helixharness-ai-result-body li::before {{ content:""; position:absolute; left:10px; top:1.22em; width:6px; height:6px; border-radius:50%; background:var(--ai-color); opacity:.65; }}
.helixharness-ai-error {{ border-color:#FFD8BF; background:#FFF9F5; }}
.helixharness-ai-error .helixharness-ai-head span {{ background:#FFF2E8; color:#D46B08; border-color:#FFD591; }}
.helixharness-ai-answer {{ margin-top:12px; color:var(--helixharness-ink); font-size:13px; line-height:1.7; }}
.helixharness-ai-answer p {{ margin:0 0 7px; }}
.helixharness-ai-answer ul {{ margin:0 0 0 18px; color:var(--helixharness-ink); }}
.helixharness-ai-answer li {{ margin:0 0 6px; }}
.helixharness-ai-result-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
.helixharness-ai-result-card {{ position:relative; min-height:88px; padding:12px 13px; border:1px solid #E5E6EB; border-radius:6px; background:#fff; overflow:hidden; }}
.helixharness-ai-result-card::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:#D9E3F0; }}
.helixharness-ai-result-conclusion {{ grid-column:1/-1; min-height:68px; background:#F8FAFD; border-color:#D9E3F0; }}
.helixharness-ai-result-blocker {{ background:#FFFDFD; border-color:#E9D4D4; }}
.helixharness-ai-result-label {{ display:flex; align-items:center; gap:7px; margin-bottom:7px; }}
.helixharness-ai-result-label i {{ display:inline-flex; width:7px; height:7px; border-radius:50%; background:var(--ai-color); opacity:.78; }}
.helixharness-ai-result-label b {{ display:block; color:#1D2129; font-size:13px; font-weight:800; }}
.helixharness-ai-result-body span {{ display:block; color:var(--helixharness-ink); font-size:13px; line-height:1.6; overflow-wrap:anywhere; }}
.helixharness-ai-result-body ul {{ margin:0; padding-left:17px; color:var(--helixharness-ink); }}
.helixharness-ai-result-body li {{ margin:0 0 5px; color:var(--helixharness-ink); font-size:12.5px; line-height:1.55; overflow-wrap:anywhere; }}
.helixharness-ai-mark {{ display:inline; padding:0 2px; margin:0; border-radius:3px; font-style:normal; font-weight:700; line-height:inherit; }}
.helixharness-ai-mark.num {{ background:#F2F7FF; color:#0958D9; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:.95em; }}
.helixharness-ai-mark.risk {{ background:#FFF4F3; color:#B42318; text-decoration:underline; text-decoration-color:#F0B8B3; text-underline-offset:2px; }}
.helixharness-ai-mark.warn {{ background:#FFF9ED; color:#8A5A00; }}
.helixharness-ai-mark.good {{ background:#F5FBF1; color:#237804; }}
.helixharness-ai-meta {{ margin-top:9px; color:#86909C; font-size:11.5px; }}
.helixharness-mini-row {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:9px; min-width:0; width:100%; }}
.helixharness-mini {{ background:#F7F9FC; border:1px solid #E5E6EB; border-radius:6px; padding:9px 10px; min-height:76px; display:flex; flex-direction:column; justify-content:space-between; min-width:0; }}
.helixharness-mini-label {{ color:var(--helixharness-muted); font-size:12px; line-height:1.25; white-space:nowrap; }}
.helixharness-mini b {{ display:block; color:var(--helixharness-ink); font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:20px; font-weight:800; line-height:1.05; margin-top:6px; white-space:normal; overflow-wrap:anywhere; }}
.helixharness-mini small {{ display:block; color:#86909C; font-size:11px; line-height:1.25; margin-top:4px; overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }}
.helixharness-mini-value.raw {{ margin-top:5px; }}
.helixharness-detail-page {{ display:none; position:fixed; z-index:9999; inset:18px; overflow:auto; padding:16px; background:#F7F8FA; border:1px solid #D9E3F0; border-radius:8px; box-shadow:0 18px 60px rgba(29,33,41,.25); }}
.helixharness-detail-page:target {{ display:block; }}
.helixharness-detail-page-head {{ position:sticky; top:-16px; z-index:3; display:flex; align-items:center; justify-content:space-between; gap:14px; margin:-16px -16px 14px; padding:14px 16px; background:#fff; border-bottom:1px solid #E5E6EB; }}
.helixharness-detail-page-head b {{ display:block; color:var(--helixharness-ink); font-size:18px; line-height:1.25; }}
.helixharness-detail-page-head span {{ display:block; color:var(--helixharness-muted); font-size:12px; margin-top:3px; }}
.helixharness-detail-page.helixharness-runtime-detail {{ inset:28px; max-width:1440px; margin:0 auto; padding:0; }}
.helixharness-runtime-detail .helixharness-detail-page-head {{ top:0; margin:0; padding:20px 24px; }}
.helixharness-runtime-detail .helixharness-detail-page-head b {{ overflow-wrap:anywhere; }}
.helixharness-runtime-detail .helixharness-detail-page-head a {{ flex-shrink:0; font-weight:500; }}
.helixharness-runtime-detail-body {{ padding:20px 24px 24px; }}
.helixharness-runtime-detail-card {{ background:#fff; border:1px solid #E5E6EB; border-radius:6px; overflow:hidden; }}
.helixharness-runtime-detail-note {{ margin:0; padding:13px 16px; color:#86909C; font-size:12px; line-height:1.6; }}
.helixharness-runtime-detail-scroll {{ overflow-x:auto; }}
.helixharness-runtime-detail-table {{ width:100%; min-width:900px; table-layout:fixed; border-collapse:collapse; text-align:left; font-size:12px; }}
.helixharness-runtime-detail-table th {{ padding:11px 16px; background:#F5F7FA; border-top:1px solid #E5E6EB; border-bottom:1px solid #E5E6EB; color:#4E5969; font-size:11px; font-weight:600; }}
.helixharness-runtime-detail-table td {{ padding:16px; border-bottom:1px solid #F0F0F0; vertical-align:middle; color:#1D2129; line-height:1.7; overflow-wrap:anywhere; }}
.helixharness-runtime-detail-table tbody tr:last-child td {{ border-bottom:0; }}
.helixharness-runtime-detail-table tbody tr:hover {{ background:#F7FAFF; }}
.helixharness-runtime-member b,.helixharness-runtime-repository {{ font-weight:600; }}
.helixharness-runtime-requirement {{ margin-top:4px; color:#86909C; font-size:11px; overflow-wrap:anywhere; }}
.helixharness-runtime-version-tag {{ display:inline-block; padding:2px 8px; border-radius:4px; color:#0958D9; background:#E8F3FF; font-size:11px; overflow-wrap:anywhere; }}
.helixharness-runtime-platform-label {{ color:#4E5969; font-size:12px; }}
.helixharness-runtime-detail .helixharness-log-date {{ color:#4E5969; }}
.helixharness-runtime-detail .helixharness-log-clock {{ margin-top:1px; color:#86909C; font-size:11px; }}
.helixharness-runtime-detail-scroll:focus-visible,.helixharness-runtime-detail .helixharness-detail-page-head a:focus-visible {{ outline:2px solid #1677FF; outline-offset:-2px; }}
@media (max-width:700px) {{
  .helixharness-detail-page.helixharness-runtime-detail {{ inset:10px; }}
  .helixharness-runtime-detail .helixharness-detail-page-head {{ padding:16px; align-items:flex-start; flex-wrap:wrap; }}
  .helixharness-runtime-detail-body {{ padding:12px; }}
}}
.helixharness-detail-page-head .helixharness-runtime-summary {{ color:#0958D9; font-weight:700; }}
.helixharness-detail-page-head a {{ display:inline-flex; align-items:center; min-height:32px; padding:0 12px; border-radius:6px; color:#0958D9 !important; background:#E8F3FF; border:1px solid #91CAFF; text-decoration:none !important; font-weight:700; font-size:12px; }}
.helixharness-detail-actions {{ display:flex; align-items:center; justify-content:flex-end; gap:8px; flex-wrap:wrap; }}
.helixharness-detail-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
.helixharness-detail-metrics {{ margin-bottom:12px; }}
.helixharness-ai-detail-page {{ background:#F6F8FB; }}
.helixharness-ai-detail-intro {{ display:flex; align-items:flex-start; gap:10px; margin:0 0 12px; padding:11px 12px; border:1px solid #D9E3F0; border-radius:6px; background:#FFFFFF; }}
.helixharness-ai-detail-intro b {{ flex:0 0 auto; color:#0958D9; font-size:12px; }}
.helixharness-ai-detail-intro span {{ color:var(--helixharness-muted); font-size:12px; line-height:1.55; }}
.helixharness-ai-detail-page .helixharness-ai-result-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
.helixharness-ai-detail-page .helixharness-ai-result-conclusion {{ grid-column:1/-1; }}
.helixharness-ai-detail-page .helixharness-ai-result-action {{ grid-column:1/-1; }}
.helixharness-ai-detail-page .helixharness-ai-result-card {{ min-height:auto; }}
.helixharness-bar {{ display:grid; grid-template-columns:repeat(var(--helixharness-stage-count,6),minmax(72px,1fr)); gap:6px; margin:0 0 12px; }}
.helixharness-seg {{ border-radius:6px; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:8px 4px; min-width:0; min-height:56px; }}
.helixharness-seg.done {{ color:white; }}
.helixharness-seg.pending {{ background:#F2F3F5; color:#86909C; }}
.helixharness-seg.current {{ outline:2px solid #FAAD14; box-shadow:0 8px 18px rgba(250,173,20,.16); }}
.helixharness-seg-num {{ display:flex; align-items:baseline; gap:2px; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:18px; font-weight:700; line-height:1; }}
.helixharness-seg-num small {{ font-size:10px; font-weight:700; }}
.helixharness-seg-lbl,.helixharness-seg-time {{ font-size:10.5px; margin-top:5px; white-space:nowrap; }}
.helixharness-stage-list {{ display:grid; gap:12px; margin-top:12px; }}
.helixharness-stage-card {{ position:relative; border:1px solid #D9E8FF; border-radius:8px; background:#FFFFFF; padding:13px; box-shadow:0 8px 22px rgba(22,119,255,.07); overflow:hidden; }}
.helixharness-stage-card::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:4px; background:#1677FF; }}
:is(.helixharness-archive-knowledge,.helixharness-implementation-files,.helixharness-archive-documents) {{ border:1px solid #D9E8FF; border-radius:6px; background:#FFF; margin-top:10px; padding:10px; }}
:is(.helixharness-archive-knowledge,.helixharness-implementation-files,.helixharness-archive-documents) > summary {{ display:flex; align-items:center; justify-content:space-between; gap:12px; cursor:pointer; list-style:none; color:#0958D9; font-size:12px; }}
:is(.helixharness-archive-knowledge,.helixharness-implementation-files,.helixharness-archive-documents) > summary::-webkit-details-marker {{ display:none; }}
.helixharness-implementation-files > summary {{ -webkit-user-select:none; user-select:none; }}
.helixharness-implementation-files .helixharness-archive-files-scroll {{ -webkit-user-select:text; user-select:text; }}
:is(.helixharness-archive-knowledge,.helixharness-implementation-files,.helixharness-archive-documents) > summary > span:first-child {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; }}
:is(.helixharness-archive-knowledge,.helixharness-implementation-files,.helixharness-archive-documents) > summary small {{ color:var(--helixharness-muted); font-size:12px; }}
:is(.helixharness-archive-knowledge,.helixharness-implementation-files,.helixharness-archive-documents)[open] > summary .helixharness-stage-attempts-toggle::before {{ content:attr(data-close) " −"; }}
.helixharness-archive-files-scroll {{ max-height:360px; overflow:auto; }}
.helixharness-archive-files-table {{ width:100%; border-collapse:collapse; font-size:12px; text-align:left; table-layout:fixed; }}
.helixharness-archive-files-table th,.helixharness-archive-files-table td {{ padding:8px; border-bottom:1px solid #EDF1F7; vertical-align:top; }}
.helixharness-archive-files-table th {{ position:sticky; top:0; background:#F5F8FC; color:var(--helixharness-muted); }}
.helixharness-archive-files-table th:first-child {{ width:65px; }}
.helixharness-archive-files-table th:last-child {{ width:70px; }}
.helixharness-archive-files-table code {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
:is(.helixharness-implementation-files,.helixharness-archive-documents) .helixharness-archive-files-table th:last-child {{ width:auto; }}
.helixharness-file-status {{ white-space:nowrap; font-size:11px; }}
.helixharness-file-status-a,.helixharness-file-added {{ color:#237804; }}
.helixharness-file-status-d,.helixharness-file-deleted {{ color:#B42318; }}
.helixharness-file-status-m,.helixharness-file-status-t {{ color:#0958D9; }}
.helixharness-archive-files-unavailable {{ padding:10px; color:var(--helixharness-muted); background:#F8FAFD; font-size:12px; }}
@media (max-width:540px) {{ .helixharness-archive-files-table th:first-child {{ width:42px; }} .helixharness-archive-files-table th:last-child {{ width:48px; }} .helixharness-archive-files-table th,.helixharness-archive-files-table td {{ padding:8px 4px; }} :is(.helixharness-archive-knowledge,.helixharness-implementation-files,.helixharness-archive-documents) > summary {{ align-items:flex-start; }} }}
.helixharness-stage-attempts {{ margin-top:12px; border-top:1px solid #D9E8FF; }}
.helixharness-stage-attempts > summary {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding:12px 0 0; cursor:pointer; list-style:none; color:#0958D9; font-size:12px; font-weight:600; }}
.helixharness-stage-attempts > summary::-webkit-details-marker {{ display:none; }}
.helixharness-stage-attempts > summary:focus-visible {{ outline:2px solid #91CAFF; outline-offset:3px; }}
.helixharness-stage-attempts-toggle::before {{ content:attr(data-open) " ＋"; }}
.helixharness-stage-attempts[open] > summary .helixharness-stage-attempts-toggle::before {{ content:attr(data-close) " −"; }}
.helixharness-stage-rollup > .helixharness-stage-summary-grid .helixharness-artifact-list {{ max-height:160px; overflow-y:auto; margin-top:6px; }}
.helixharness-stage-card.prior-attempt {{ border-color:#E5E6EB; box-shadow:none; }}
.helixharness-stage-card.prior-attempt::before {{ background:#B4BBC6; }}
.helixharness-stage-card.latest-attempt {{ border-color:#91CAFF; }}
.helixharness-stage-card-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin:0 0 8px; }}
.helixharness-stage-card-head > div:first-child {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px; min-width:0; }}
.helixharness-stage-card-head b {{ color:var(--helixharness-ink); font-size:14px; }}
.helixharness-stage-card-head code {{ color:#0958D9; background:#E8F3FF; border:1px solid #BAE0FF; border-radius:4px; padding:2px 6px; font-size:11px; max-width:100%; overflow-wrap:anywhere; }}
.helixharness-stage-index {{ display:inline-flex; align-items:center; justify-content:center; width:22px; height:22px; border-radius:50%; background:#1677FF; color:#fff; font-size:12px; font-weight:800; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; }}
.helixharness-stage-head-meta {{ display:flex; align-items:center; justify-content:flex-end; flex-wrap:wrap; gap:7px; flex:0 0 auto; }}
.helixharness-stage-head-meta b {{ color:#0958D9; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:13px; }}
.helixharness-attempt-pill {{ display:inline-flex; align-items:center; min-height:20px; padding:1px 7px; border-radius:999px; background:#F2F3F5; color:#4E5969; font-size:11px; font-weight:700; }}
.helixharness-attempt-pill.rerun {{ background:#FFF7E8; color:#AD6800; }}
.helixharness-attempt-pill.continuation {{ background:#E8FFFB; color:#007A72; }}
.helixharness-attempt-pill.latest {{ background:#E8F3FF; color:#0958D9; }}
.helixharness-stage-meta {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px 14px; color:var(--helixharness-muted); font-size:12px; margin:0 0 11px; padding-bottom:10px; border-bottom:1px solid #F0F5FF; }}
.helixharness-stage-grid {{ display:grid; gap:9px; min-width:0; }}
.helixharness-stage-summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:9px; min-width:0; }}
.helixharness-stage-block {{ min-width:0; padding:10px; border:1px solid #E5E6EB; border-radius:6px; background:#F8FAFD; line-height:1.55; }}
.helixharness-stage-block-wide {{ grid-column:span 2; }}
.helixharness-stage-block h4 {{ margin:0 0 7px; color:#1D4ED8; font-size:12px; font-weight:800; }}
.helixharness-stage-block span,.helixharness-stage-block small {{ color:var(--helixharness-muted); font-size:12px; overflow-wrap:anywhere; }}
.helixharness-stage-block b {{ color:var(--helixharness-ink); }}
.helixharness-stage-block hr {{ border:0; border-top:1px solid #E5E6EB; margin:7px 0; }}
.helixharness-artifact-list {{ display:grid; gap:5px; margin:0; padding:0; list-style:none; }}
.helixharness-artifact-list li {{ min-width:0; }}
.helixharness-artifact-list code {{ display:block; width:100%; color:#0958D9; background:#FFFFFF; border:1px solid #D9E8FF; border-radius:4px; padding:4px 6px; font-size:11px; white-space:normal; overflow-wrap:anywhere; }}
.helixharness-async-pending {{ border-color:#FFE0A3; background:#FFF9ED; }}
.helixharness-async-pending h4 {{ color:#AD6800; }}
.helixharness-stage-exception {{ border-color:#E5E6EB; background:#F8FAFD; }}
.helixharness-stage-exception > summary {{ cursor:pointer; color:var(--helixharness-muted); font-size:12px; font-weight:600; }}
.helixharness-stage-exception[open] > summary {{ margin-bottom:8px; }}
.helixharness-stage-exception-note {{ display:inline-block; margin-top:5px; color:#7A4B47 !important; }}
.helixharness-stage-effective {{ grid-column:1/-1; display:flex; align-items:flex-start; justify-content:space-between; gap:14px; padding:9px 10px; border:1px dashed #91CAFF; border-radius:6px; background:#F2F8FF; color:#4E5969; font-size:11.5px; }}
.helixharness-stage-effective > span {{ color:#0958D9; font-weight:800; white-space:nowrap; }}
.helixharness-stage-effective > div {{ display:flex; justify-content:flex-end; gap:8px 18px; flex-wrap:wrap; text-align:right; }}
.helixharness-stage-effective b {{ color:#0958D9; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:11.5px; }}
.helixharness-stage-notes {{ display:flex; gap:8px; align-items:flex-start; margin-top:10px; padding:9px 10px; border-radius:6px; background:#F5F7FA; color:var(--helixharness-muted); font-size:12px; line-height:1.45; }}
.helixharness-stage-notes b {{ flex:0 0 auto; color:var(--helixharness-ink); }}
.helixharness-stage-notes span {{ overflow-wrap:anywhere; }}
.helixharness-tip-wrap {{ position:relative; display:inline-flex; align-items:center; gap:4px; overflow:visible; vertical-align:middle; z-index:20; }}
.helixharness-tip-wrap:hover,.helixharness-tip-wrap:focus-within {{ z-index:10050; }}
.helixharness-tip {{ display:inline-flex; align-items:center; justify-content:center; width:14px; height:14px; border-radius:50%; background:#F2F7FF; border:1px solid #B7D8FF; color:#1677FF; font-size:10px; font-weight:700; }}
.helixharness-tip-box {{ display:none; position:absolute; left:0; top:22px; z-index:10060; width:max-content; min-width:96px; max-width:min(280px,calc(100vw - 48px)); padding:8px 10px; border-radius:6px; border:1px solid #D9E3F0; background:#fff; color:var(--helixharness-ink) !important; box-shadow:0 14px 34px rgba(29,33,41,.18); font-size:12px !important; line-height:1.5; white-space:normal; overflow-wrap:anywhere; text-align:left; }}
.helixharness-metric:nth-child(3n) .helixharness-tip-box,.helixharness-metric:last-child .helixharness-tip-box {{ left:auto; right:0; }}
.helixharness-tip-wrap:hover .helixharness-tip-box,.helixharness-tip:focus + .helixharness-tip-box {{ display:block; }}
.helixharness-empty-mini,.helixharness-empty {{ color:var(--helixharness-muted); background:#F5F7FA; border:1px solid #E5E6EB; border-radius:6px; padding:12px; font-size:12px; }}
@media (max-width:900px) {{ .helixharness-detail-grid {{ grid-template-columns:1fr; }} .helixharness-bar {{ grid-template-columns:repeat(3,1fr); }} .helixharness-detail-page {{ inset:8px; }} .helixharness-ai-entry-panel {{ align-items:flex-start; flex-direction:column; }} .helixharness-ai-team-grid,.helixharness-ai-scope-feature .helixharness-ai-result-grid,.helixharness-ai-detail-page .helixharness-ai-result-grid,.helixharness-stage-summary-grid {{ grid-template-columns:1fr; }} .helixharness-ai-team-main,.helixharness-ai-scope-feature .helixharness-ai-result-conclusion,.helixharness-ai-scope-feature .helixharness-ai-result-action,.helixharness-ai-detail-page .helixharness-ai-result-flow,.helixharness-ai-detail-page .helixharness-ai-result-action {{ grid-column:auto; }} .helixharness-stage-block-wide {{ grid-column:auto; }} .helixharness-stage-exception {{ display:block; }} .helixharness-stage-card-head {{ flex-direction:column; }} .helixharness-stage-head-meta {{ justify-content:flex-start; }} }}
</style>
"""
    return (
        f'{style}<div class="helixharness-usage-root" id="helixharness-usage-top">'
        f'{overview_panel}'
        f'{_team_ai_placeholder_html(all_agg["summary"], all_agg["features"], _summarize_user_rows(all_agg["user_project"]), ai_insights)}'
        f'</div>'
    ) if view_mode == "__global_overview__" else (
        f'{style}<div class="helixharness-usage-root" id="helixharness-usage-top">'
        f'{_display_rules_html()}'
        f'<div class="helixharness-panel helixharness-dimension-panel"><div class="helixharness-section-head helixharness-dimension-head"><div><div class="helixharness-section-hint">{_esc(active_hint)}</div></div></div>{active_table}</div>'
        f'{feature_pages}{installation_pages}'
        f'</div>'
    )

    mode_key = _view_mode_key(view_mode)
    if mode_key == "project":
        rows = agg["project_view"]
        main_title = "按项目维度"
        main_hint = "当前页面主列表只展示项目；点开某个项目后查看参与用户和需求明细。"
        main_table = _render_project_view(rows, agg["user_project"], agg["features"])
    else:
        rows = agg["user_project"]
        main_title = "按用户维度"
        main_hint = "当前页面主列表只展示用户；点开某个用户后查看涉及项目和需求明细。"
        main_table = _render_user_view(rows, agg["features"])

    cards = _dimension_cards(s, rows, mode_key) if s["feature_total"] else ""
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    roots_hint = " · ".join(os.path.basename(os.path.normpath(r)) for r in roots)

    style = f"""
<style>
.helixharness-usage-root {{
  --helixharness-panel:#FFFFFF;
  --helixharness-ink:{_TEXT};
  --helixharness-muted:{_MUTED};
  --helixharness-border:{_BORDER};
  --helixharness-accent:{_ACCENT};
  --helixharness-green:{_GREEN};
  --helixharness-gold:{_GOLD};
  --helixharness-red:{_RED};
  color:var(--helixharness-ink);
  font-family:"PingFang SC","Microsoft YaHei","Helvetica Neue",Arial,sans-serif;
  width:100%;
  max-width:none;
}}
.helixharness-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:10px; margin:0 0 14px; }}
.helixharness-metric {{ position:relative; overflow:visible; background:#FFFFFF; border:1px solid var(--helixharness-border); border-radius:6px; padding:12px 14px 11px; min-height:88px; box-shadow:0 1px 2px rgba(29,33,41,.04); }}
.helixharness-metric::before {{ content:""; position:absolute; inset:0 auto 0 0; width:3px; background:var(--metric); }}
.helixharness-metric-head {{ display:flex; align-items:center; justify-content:space-between; gap:8px; color:var(--helixharness-muted); font-size:12px; font-weight:600; letter-spacing:0; }}
.helixharness-metric-head i {{ width:22px; height:2px; border-radius:999px; background:var(--metric); opacity:.75; }}
.helixharness-metric-num {{ margin-top:12px; color:var(--helixharness-ink); font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:30px; font-weight:800; line-height:1; letter-spacing:0; }}
.helixharness-metric-sub {{ margin-top:8px; min-height:14px; color:var(--helixharness-muted); font-size:11.5px; line-height:1.25; }}
.helixharness-layout {{ display:grid; grid-template-columns:1fr; gap:12px; margin-bottom:16px; }}
.helixharness-panel {{ background:var(--helixharness-panel); border:1px solid var(--helixharness-border); border-radius:6px; padding:14px; box-shadow:0 1px 2px rgba(29,33,41,.04); overflow:visible; }}
.helixharness-section-head {{ display:flex; justify-content:space-between; gap:18px; align-items:flex-start; margin:0 0 14px; padding:3px 2px 12px; border-bottom:1px solid #F0F0F0; }}
.helixharness-section-title {{ font-weight:600; font-size:14px; color:var(--helixharness-ink); letter-spacing:0; }}
.helixharness-section-hint {{ color:var(--helixharness-muted); font-size:12.5px; line-height:1.55; max-width:560px; }}
.helixharness-table {{ width:100%; border-collapse:separate; border-spacing:0; font-size:12.5px; background:#FFFFFF; table-layout:auto; }}
.helixharness-table th {{ text-align:left; padding:9px 10px; color:var(--helixharness-muted); background:#F5F7FA; border-bottom:1px solid var(--helixharness-border); font-size:12px; font-weight:600; letter-spacing:0; white-space:nowrap; }}
.helixharness-table th:has(.helixharness-tip-wrap:hover),.helixharness-table th:has(.helixharness-tip-wrap:focus-within) {{ position:relative; z-index:10040; }}
.helixharness-table td {{ padding:10px; border-bottom:1px solid #F0F0F0; vertical-align:top; color:var(--helixharness-ink); }}
.helixharness-table tr:last-child td {{ border-bottom:0; }}
.helixharness-table td span {{ color:var(--helixharness-muted); font-size:11.5px; }}
.helixharness-table code {{ color:#0958D9; background:#E8F3FF; border:1px solid #BAE0FF; border-radius:4px; padding:1px 5px; font-size:11px; }}
.helixharness-num {{ font-weight:700; color:var(--helixharness-accent); text-align:center; font-size:15px; }}
.helixharness-badge {{ display:inline-flex; align-items:center; justify-content:center; min-height:18px; padding:2px 8px; border-radius:999px; font-size:10.5px; font-weight:600; white-space:nowrap; margin:1px 2px 1px 0; }}
.helixharness-pill,.helixharness-muted-pill {{ display:inline-flex; align-items:center; padding:2px 7px; border-radius:999px; font-size:10.5px; margin:1px 3px 1px 0; white-space:nowrap; font-weight:600; }}
.helixharness-muted-pill {{ background:#F2F3F5; color:#86909C; }}
.helixharness-tip-wrap {{ position:relative; display:inline-flex; align-items:center; gap:4px; overflow:visible; vertical-align:middle; isolation:isolate; }}
.helixharness-tip-wrap:hover,.helixharness-tip-wrap:focus-within {{ z-index:5000; }}
.helixharness-tip {{ display:inline-flex; align-items:center; justify-content:center; width:14px; height:14px; border-radius:50%; background:#F2F7FF; border:1px solid #B7D8FF; color:var(--helixharness-accent); font-size:10px; font-weight:700; line-height:1; font-style:normal; cursor:help; text-transform:none; letter-spacing:0; transform:translateY(-.5px); }}
.helixharness-tip-box {{ visibility:hidden; opacity:0; position:absolute; left:50%; top:24px; z-index:4000; width:min(340px,calc(100vw - 48px)); max-width:calc(100vw - 48px); padding:10px 11px; border-radius:6px; border:1px solid #D9E3F0; background:#FFFFFF; color:var(--helixharness-ink) !important; box-shadow:0 14px 34px rgba(29,33,41,.18); font-size:12px !important; line-height:1.55; text-transform:none; letter-spacing:0; transition:opacity .12s ease,transform .12s ease; transform:translate(-50%,-3px); white-space:normal; overflow-wrap:anywhere; text-align:left; }}
.helixharness-tip-box::before {{ content:""; position:absolute; left:50%; top:-6px; width:10px; height:10px; transform:translateX(-50%) rotate(45deg); background:#FFFFFF; border-left:1px solid #D9E3F0; border-top:1px solid #D9E3F0; }}
.helixharness-tip-wrap:hover .helixharness-tip-box,.helixharness-tip:focus + .helixharness-tip-box {{ visibility:visible; opacity:1; transform:translate(-50%,0); }}
.helixharness-drill {{ border:1px solid var(--helixharness-border); border-radius:8px; background:#FFFFFF; margin-bottom:12px; overflow:visible; box-shadow:0 1px 0 rgba(23,33,30,.035); transition:border-color .16s ease,box-shadow .16s ease,transform .16s ease; }}
.helixharness-drill:hover {{ border-color:#91CAFF; box-shadow:0 10px 28px rgba(22,119,255,.10); transform:translateY(-1px); }}
.helixharness-drill[open] {{ border-color:#BCD9D1; box-shadow:0 12px 34px rgba(15,118,110,.09); transform:none; }}
.helixharness-drill-summary {{ position:relative; display:grid; grid-template-columns:minmax(320px,1fr) auto; grid-template-areas:"title open" "metrics metrics"; gap:12px 18px; align-items:start; padding:14px 14px 14px 18px; cursor:pointer; list-style:none; border-left:4px solid transparent; border-radius:8px; }}
.helixharness-drill-summary::-webkit-details-marker {{ display:none; }}
.helixharness-drill-summary:hover {{ background:#F2F7FF; border-left-color:#1677FF; }}
.helixharness-drill[open] .helixharness-drill-summary {{ border-left-color:#52C41A; border-radius:8px 8px 0 0; }}
.helixharness-drill-title {{ grid-area:title; display:flex; flex-direction:column; align-items:flex-start; gap:7px; min-width:0; }}
.helixharness-identity-line {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px; min-width:0; }}
.helixharness-drill-title b {{ color:var(--helixharness-ink); font-size:14px; line-height:1.2; }}
.helixharness-drill-title code {{ width:max-content; max-width:100%; color:#0958D9; background:#E8F3FF; border:1px solid #BAE0FF; border-radius:4px; padding:2px 6px; font-size:11px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.helixharness-drill-title .helixharness-badge {{ align-self:flex-start; }}
.helixharness-row-summary {{ color:var(--helixharness-ink); font-size:13px; line-height:1.45; font-weight:600; margin-top:0; }}
.helixharness-focus-tags {{ display:flex; align-items:center; flex-wrap:wrap; gap:5px; margin-top:5px; }}
.helixharness-focus-tag {{ display:inline-flex; align-items:center; height:22px; padding:0 8px; border-radius:999px; font-size:11px; font-weight:600; white-space:nowrap; }}
.helixharness-mini-row {{ grid-area:metrics; display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:9px; min-width:0; width:100%; }}
.helixharness-adoption-mini {{ grid-area:auto; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); margin:0 0 12px; }}
.helixharness-adoption-summary {{ display:flex; align-items:flex-start; gap:8px; flex-wrap:wrap; padding:9px 0; border-top:1px solid #F0F0F0; color:var(--helixharness-muted); font-size:12px; }}
.helixharness-adoption-summary b {{ color:var(--helixharness-ink); min-width:62px; }}
.helixharness-command-wrap {{ display:flex; flex-wrap:wrap; gap:3px; max-width:420px; }}
.helixharness-adoption-inline {{ display:flex; align-items:center; flex-wrap:wrap; gap:5px; margin-top:2px; color:var(--helixharness-muted); font-size:11.5px; line-height:1.45; }}
.helixharness-adoption-inline code {{ max-width:100%; color:#0958D9; background:#E8F3FF; border:1px solid #BAE0FF; border-radius:4px; padding:1px 5px; font-size:10.5px; white-space:normal; }}
.helixharness-mini {{ background:#F7F9FC; border:1px solid #E5E6EB; border-radius:6px; padding:9px 10px; min-height:76px; display:flex; flex-direction:column; justify-content:space-between; min-width:0; }}
.helixharness-mini-label {{ display:flex; align-items:center; min-width:0; color:var(--helixharness-muted); font-size:12px; line-height:1.25; white-space:nowrap; }}
.helixharness-mini b {{ display:block; color:var(--helixharness-ink); font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:20px; font-weight:800; line-height:1.05; letter-spacing:0; margin-top:6px; white-space:normal; overflow-wrap:anywhere; }}
.helixharness-mini b .helixharness-badge {{ font-family:"PingFang SC","Microsoft YaHei","Helvetica Neue",Arial,sans-serif; }}
.helixharness-mini > span {{ display:block; color:var(--helixharness-muted); font-size:12px; white-space:nowrap; }}
.helixharness-mini small {{ display:block; color:#86909C; font-size:11px; line-height:1.25; margin-top:4px; overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }}
.helixharness-mini-value.raw {{ margin-top:5px; }}
.helixharness-open-hint {{ grid-area:open; align-self:start; justify-self:end; display:inline-flex; align-items:center; gap:8px; min-height:32px; color:#0958D9; background:#E8F3FF; border:1px solid #91CAFF; border-radius:6px; padding:0 10px 0 12px; font-size:12px; font-weight:700; white-space:nowrap; box-shadow:0 3px 10px rgba(22,119,255,.10); transition:background .16s ease,border-color .16s ease,color .16s ease,box-shadow .16s ease; }}
.helixharness-open-hint::before {{ content:attr(data-open); }}
.helixharness-open-hint::after {{ content:"›"; display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px; border-radius:50%; color:#FFFFFF; background:#1677FF; font-size:15px; line-height:1; transition:transform .16s ease,background .16s ease; }}
.helixharness-drill-summary:hover .helixharness-open-hint,.helixharness-feature-summary:hover .helixharness-open-hint {{ background:#D6EFFF; border-color:#69B1FF; box-shadow:0 6px 16px rgba(22,119,255,.16); }}
.helixharness-drill[open] > .helixharness-drill-summary .helixharness-open-hint,.helixharness-feature-drill[open] > .helixharness-feature-summary .helixharness-open-hint {{ color:#237804; background:#F6FFED; border-color:#95DE64; box-shadow:none; }}
.helixharness-drill[open] > .helixharness-drill-summary .helixharness-open-hint::before,.helixharness-feature-drill[open] > .helixharness-feature-summary .helixharness-open-hint::before {{ content:attr(data-close); }}
.helixharness-drill[open] > .helixharness-drill-summary .helixharness-open-hint::after,.helixharness-feature-drill[open] > .helixharness-feature-summary .helixharness-open-hint::after {{ background:#52C41A; transform:rotate(90deg); }}
.helixharness-drill-body {{ border-top:1px solid var(--helixharness-border); padding:14px; background:#F7F8FA; overflow:visible; }}
.helixharness-subtitle {{ display:flex; align-items:center; gap:8px; margin:3px 0 9px; color:var(--helixharness-ink); font-weight:600; font-size:12px; letter-spacing:0; }}
.helixharness-subtitle::before {{ content:""; width:16px; height:2px; border-radius:999px; background:var(--helixharness-accent); }}
.helixharness-subtitle:not(:first-child) {{ margin-top:17px; }}
.helixharness-nested-drill {{ margin-top:14px; border:1px solid #D9E3F0; border-radius:6px; background:#FFFFFF; overflow:visible; }}
.helixharness-nested-drill > summary {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px 14px; align-items:center; min-height:46px; padding:10px 12px; cursor:pointer; list-style:none; border-left:4px solid #69B1FF; border-radius:6px; }}
.helixharness-nested-drill > summary::-webkit-details-marker {{ display:none; }}
.helixharness-nested-drill > summary:hover {{ background:#F2F7FF; }}
.helixharness-nested-drill > summary > span {{ display:flex; align-items:center; gap:8px; color:var(--helixharness-ink); font-size:13px; font-weight:700; }}
.helixharness-nested-drill > summary > span em {{ display:inline-flex; align-items:center; height:20px; padding:0 7px; border-radius:999px; background:#E8F3FF; color:#0958D9; font-style:normal; font-size:10.5px; font-weight:800; }}
.helixharness-nested-drill > summary small {{ display:block; color:var(--helixharness-muted); font-size:11.5px; line-height:1.35; margin-top:2px; }}
.helixharness-nested-drill > summary > b {{ display:inline-flex; align-items:center; gap:7px; color:#0958D9; background:#E8F3FF; border:1px solid #91CAFF; border-radius:6px; padding:6px 10px; font-size:12px; white-space:nowrap; }}
.helixharness-nested-drill > summary > b::before {{ content:attr(data-open); }}
.helixharness-nested-drill > summary > b::after {{ content:"›"; display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px; border-radius:50%; color:#FFFFFF; background:#1677FF; font-size:15px; line-height:1; transition:transform .16s ease,background .16s ease; }}
.helixharness-nested-drill[open] > summary {{ border-left-color:#52C41A; border-radius:6px 6px 0 0; }}
.helixharness-nested-drill[open] > summary > b {{ color:#237804; background:#F6FFED; border-color:#95DE64; }}
.helixharness-nested-drill[open] > summary > b::before {{ content:attr(data-close); }}
.helixharness-nested-drill[open] > summary > b::after {{ background:#52C41A; transform:rotate(90deg); }}
.helixharness-nested-body {{ position:relative; padding:12px 12px 12px 22px; border-top:1px solid #E5E6EB; background:#F7F8FA; }}
.helixharness-nested-body::before {{ content:""; position:absolute; left:11px; top:12px; bottom:12px; width:2px; border-radius:999px; background:#BAE0FF; }}
.helixharness-detail-table {{ border:1px solid #E5E6EB; border-radius:6px; overflow:visible; box-shadow:0 1px 2px rgba(29,33,41,.04); }}
.helixharness-detail-table th {{ font-size:12.5px; }}
.helixharness-detail-table td {{ padding:13px 14px; }}
.helixharness-cell-title {{ color:var(--helixharness-ink); font-weight:700; font-size:14px; line-height:1.35; }}
.helixharness-feature-cell-title {{ display:flex; align-items:center; flex-wrap:wrap; gap:7px; margin-bottom:3px; }}
.helixharness-cell-sub {{ color:var(--helixharness-muted); font-size:12px; line-height:1.5; margin-top:3px; }}
.helixharness-feature-list {{ display:grid; gap:9px; }}
.helixharness-feature-drill {{ position:relative; border:1px solid #E5E6EB; border-radius:6px; background:#FFFFFF; overflow:visible; transition:border-color .16s ease,box-shadow .16s ease,transform .16s ease; }}
.helixharness-feature-drill::before {{ content:""; position:absolute; left:-12px; top:22px; width:12px; height:2px; background:#BAE0FF; }}
.helixharness-feature-drill:hover {{ border-color:#91CAFF; box-shadow:0 8px 20px rgba(22,119,255,.08); transform:translateY(-1px); }}
.helixharness-feature-drill[open] {{ border-color:#91CAFF; box-shadow:0 8px 20px rgba(22,119,255,.08); transform:none; }}
.helixharness-feature-summary {{ display:grid; grid-template-columns:minmax(0,1fr) auto; grid-template-areas:"title open" "metrics metrics"; gap:12px; align-items:start; padding:13px 13px 14px 17px; cursor:pointer; list-style:none; border-left:4px solid transparent; border-radius:6px; }}
.helixharness-feature-summary::-webkit-details-marker {{ display:none; }}
.helixharness-feature-summary:hover {{ background:#F2F7FF; border-left-color:#1677FF; }}
.helixharness-feature-drill[open] .helixharness-feature-summary {{ border-left-color:#52C41A; border-radius:6px 6px 0 0; }}
.helixharness-feature-title-line {{ display:flex; align-items:center; gap:8px; min-width:0; }}
.helixharness-level-badge {{ display:inline-flex; align-items:center; height:21px; padding:0 7px; border-radius:999px; background:#F6FFED; color:#237804; border:1px solid #B7EB8F; font-size:10.5px; font-weight:800; white-space:nowrap; }}
.helixharness-feature-summary-tags {{ display:flex; align-items:center; flex-wrap:wrap; gap:4px; margin-top:1px; }}
.helixharness-feature-mini {{ grid-area:metrics; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); padding-top:10px; border-top:1px solid #F0F0F0; }}
.helixharness-feature-body {{ padding:12px; border-top:1px solid #E5E6EB; background:#F7F8FA; }}
.helixharness-bar {{ display:grid; grid-template-columns:repeat(6,minmax(72px,1fr)); gap:6px; margin:0 0 12px; }}
.helixharness-seg {{ border-radius:6px; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:8px 4px; min-width:0; min-height:56px; }}
.helixharness-seg.done {{ color:white; }}
.helixharness-seg.pending {{ background:#F2F3F5; color:#86909C; }}
.helixharness-seg.current {{ outline:2px solid var(--helixharness-gold); box-shadow:0 8px 18px rgba(250,173,20,.16); }}
.helixharness-seg-num {{ font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:18px; font-weight:700; line-height:1; }}
.helixharness-seg-lbl {{ font-size:10.5px; margin-top:5px; white-space:nowrap; }}
.helixharness-seg-time {{ font-size:10px; margin-top:3px; opacity:.9; white-space:nowrap; }}
.helixharness-process-table {{ min-width:1120px; }}
.helixharness-process-table th {{ background:#F5F7FA; }}
.helixharness-process-cell {{ min-width:150px; line-height:1.55; }}
.helixharness-process-cell b {{ color:var(--helixharness-ink); }}
.helixharness-process-cell hr {{ border:0; border-top:1px solid #E5E6EB; margin:7px 0; }}
.helixharness-breakdown-line {{ margin-top:5px; color:#86909C; font-size:11.5px; line-height:1.45; }}
.helixharness-guide {{ margin:0 0 14px; border:1px solid var(--helixharness-border); border-radius:6px; background:#FFFFFF; box-shadow:0 1px 2px rgba(29,33,41,.04); }}
.helixharness-guide summary {{ cursor:pointer; list-style:none; padding:12px 14px; color:var(--helixharness-ink); font-size:13px; font-weight:700; }}
.helixharness-guide summary::-webkit-details-marker {{ display:none; }}
.helixharness-guide summary::after {{ content:"v"; float:right; color:var(--helixharness-accent); }}
.helixharness-guide[open] summary::after {{ content:"^"; color:var(--helixharness-green); }}
.helixharness-guide-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:10px; padding:0 14px 14px; }}
.helixharness-guide-item {{ display:grid; gap:4px; padding:10px 11px; border:1px solid #E5E6EB; border-radius:6px; background:#F7F9FC; }}
.helixharness-guide-item b {{ color:var(--helixharness-ink); font-size:12px; }}
.helixharness-guide-item span {{ color:var(--helixharness-muted); font-size:12px; line-height:1.55; }}
.helixharness-empty-mini {{ color:var(--helixharness-muted); background:#F5F7FA; border:1px solid #E5E6EB; border-radius:6px; padding:10px 12px; font-size:12px; }}
.helixharness-empty {{ background:#FFFFFF; border:1px solid var(--helixharness-border); border-radius:6px; padding:24px; color:var(--helixharness-muted); line-height:1.7; }}
.helixharness-empty-guide {{ text-align:center; padding:36px 24px; }}
.helixharness-empty-title {{ color:var(--helixharness-ink); font-size:15px; font-weight:700; margin-bottom:6px; }}
.helixharness-empty-desc {{ color:var(--helixharness-muted); font-size:12px; margin-bottom:18px; }}
.helixharness-empty-steps {{ display:flex; flex-direction:column; gap:10px; max-width:420px; margin:0 auto; text-align:left; }}
.helixharness-empty-steps span {{ display:flex; align-items:center; color:var(--helixharness-ink); font-size:12px; }}
.helixharness-step-num {{ display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px; border-radius:50%; background:var(--helixharness-accent); color:#FFFFFF; font-size:11px; font-weight:700; margin-right:8px; flex-shrink:0; }}
.helixharness-empty-steps code {{ background:#F5F7FA; padding:1px 5px; border-radius:3px; font-size:11px; color:var(--helixharness-accent); }}
.helixharness-footer {{ color:var(--helixharness-muted); font-size:11px; margin:14px 2px 0; }}
@media (max-width:1120px) {{ .helixharness-grid {{ grid-template-columns:repeat(2,minmax(140px,1fr)); }} .helixharness-drill-summary {{ grid-template-columns:1fr; grid-template-areas:"title" "metrics" "open"; }} .helixharness-feature-summary {{ grid-template-columns:1fr; grid-template-areas:"title" "open"; }} .helixharness-open-hint {{ justify-self:start; }} .helixharness-mini-row,.helixharness-feature-mini,.helixharness-adoption-mini {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .helixharness-bar {{ grid-template-columns:repeat(3,1fr); }} .helixharness-guide-grid {{ grid-template-columns:1fr; }} }}
@media (max-width:680px) {{ .helixharness-grid {{ grid-template-columns:1fr; }} .helixharness-table {{ font-size:12px; }} .helixharness-panel {{ padding:10px; }} }}
</style>
"""
    main_panel = (f'<div class="helixharness-panel"><div class="helixharness-section-head"><div class="helixharness-section-title">{_esc(main_title)}</div>'
                  f'<div class="helixharness-section-hint">{_esc(main_hint)}</div></div>{main_table}</div>')
    return (f'{style}<div class="helixharness-usage-root"><div class="helixharness-grid">{cards}</div>'
            f'{_metric_guide_html()}<div class="helixharness-layout">{main_panel}</div>'
            f'<div class="helixharness-footer">数据更新于 {updated_at} · 数据源: {_esc(roots_hint)} · '
            'HelixHarness Ledger 1.0</div></div>')
