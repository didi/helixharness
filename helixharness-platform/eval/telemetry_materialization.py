"""Join HelixHarness fixed ledgers with their independently delivered async facts.

The Git ``eval-log.json`` is the activation authority.  A Supplement may be
stored earlier, but it is not visible to dashboard or evolution consumers until
its exact fixed entry is discovered.  The discovery timestamp is therefore the
association's effective time, independent of delivery order.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from eval.stage_supplements import LEDGER_SCHEMA, LinkedStageSupplement


def index_linked_supplements(
    records: Iterable[LinkedStageSupplement],
) -> dict[str, dict[str, LinkedStageSupplement]]:
    """Index active revisions by resolved eval-log path and fixed entry id."""

    result: dict[str, dict[str, LinkedStageSupplement]] = {}
    for record in records:
        entry_id = str(record.payload.get("entry_id") or "")
        if entry_id:
            result.setdefault(record.source_path, {})[entry_id] = record
    return result


def materialized_hash(raw: bytes, records: Mapping[str, LinkedStageSupplement]) -> str:
    """Identify the combined fixed-ledger and active-Supplement projection."""

    identity = {
        "materializer": "helixharness-eval-materialized/2.0",
        "supplements": {
            entry_id: {
                "payload_hash": record.payload_hash,
                "effective_at": record.effective_at,
            }
            for entry_id, record in sorted(records.items())
        },
    }
    digest = hashlib.sha256()
    digest.update(raw)
    digest.update(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def materialize_eval_log(
    data: dict[str, Any],
    records: Mapping[str, LinkedStageSupplement] | None = None,
) -> dict[str, Any]:
    """Return the current HelixHarness ledger consumer view."""

    if data.get("schema") != LEDGER_SCHEMA:
        raise ValueError("unsupported telemetry schema")

    linked = records or {}
    result = deepcopy(data)
    entries: list[dict[str, Any]] = []
    effective_times: list[str] = []
    unavailable_metrics: set[str] = set()
    for raw_entry in data.get("entries", []):
        if not isinstance(raw_entry, dict):
            entries.append(raw_entry)
            continue
        entry_id = str(raw_entry.get("entry_id") or "")
        record = linked.get(entry_id)
        supplement = record.payload if record is not None else None
        if record is not None:
            effective_times.append(record.effective_at)
        else:
            unavailable_metrics.add(f"{entry_id}.async_supplement")
        entries.append(_materialize_entry(raw_entry, supplement, record))

    archived = data.get("status") == "archived"
    all_linked = bool(entries) and all(entry.get("telemetry_status") == "complete" for entry in entries)
    expected_stages = {"requirement", "coding", "archive"}
    observed_stages = {
        str(entry.get("stage_kind"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("stage_kind")
    }
    missing_stages = sorted(expected_stages - observed_stages)
    result["entries"] = entries
    result["collection_state"] = {
        "coverage": "partial" if missing_stages else "observed_from_start",
        "reason": "missing_workflow_stages" if missing_stages else None,
        "collection_started_at": data.get("created_at"),
        "pre_enable_stages_missing": missing_stages,
        "first_observed_event": {},
        "sealed_at": data.get("last_updated") if archived else None,
        "sealed_by_entry_id": next(
            (
                entry.get("entry_id")
                for entry in reversed(entries)
                if entry.get("stage_kind") == "archive" and entry.get("status") == "completed"
            ),
            None,
        ),
    }
    if archived and all_linked:
        finalization_status = "complete"
    elif archived:
        finalization_status = "complete_with_unavailable"
    else:
        finalization_status = "pending"
    result["finalization"] = {
        "status": finalization_status,
        "unavailable_metrics": sorted(unavailable_metrics),
    }
    result["association_effective_at"] = min(effective_times) if effective_times else None
    result["_materialized_schema"] = "helixharness-eval-materialized/2.0"
    return result


def _materialize_entry(
    fixed: dict[str, Any],
    supplement: dict[str, Any] | None,
    record: LinkedStageSupplement | None,
) -> dict[str, Any]:
    result = deepcopy(fixed)
    entry_id = str(fixed.get("entry_id") or "")
    usage = supplement.get("usage") if isinstance(supplement, dict) and isinstance(supplement.get("usage"), dict) else {}
    execution = (
        supplement.get("execution")
        if isinstance(supplement, dict) and isinstance(supplement.get("execution"), dict)
        else {}
    )
    evidence = (
        supplement.get("evidence")
        if isinstance(supplement, dict) and isinstance(supplement.get("evidence"), dict)
        else {}
    )
    trace = (
        supplement.get("workflow_trace")
        if isinstance(supplement, dict) and isinstance(supplement.get("workflow_trace"), dict)
        else {}
    )
    artifacts = fixed.get("artifacts")
    if isinstance(artifacts, list):
        result["artifacts"] = {"produced_files": [str(item) for item in artifacts]}
    elif not isinstance(artifacts, dict):
        result["artifacts"] = {"produced_files": []}

    result["telemetry_status"] = "complete" if supplement is not None else "pending"
    result["phase"] = fixed.get("phase") or fixed.get("command")
    result["stage_profile"] = fixed.get("stage_profile") or fixed.get("command")
    result["outcome_reason"] = fixed.get("end_reason")
    result["invocation"] = {
        "invocation_id": entry_id,
        "platform": fixed.get("platform"),
        "surface": fixed.get("platform"),
        "trigger": fixed.get("trigger"),
        "mode": "loop" if fixed.get("trigger") == "loop" else "manual",
        "loop": fixed.get("trigger") == "loop",
        "tdd": False,
        "loop_max_turns": None,
    }
    result["session_window"] = {
        "session_id": fixed.get("session_hash"),
        "started_at": fixed.get("started_at"),
        "ended_at": fixed.get("ended_at"),
    }
    result["workflow"] = dict(fixed.get("workflow") or {})
    result["quality"] = _materialize_quality(fixed.get("quality"), supplement is not None, trace)
    result["resource_usage"] = _materialize_usage(usage)
    result["execution_stats"] = dict(execution)
    result["collection"] = _materialize_collection(evidence)
    result["friction"] = _materialize_friction(evidence)
    result["association_effective_at"] = record.effective_at if record is not None else None
    result["supplement_received_at"] = record.received_at if record is not None else None
    result["async_evidence"] = _evolution_evidence(evidence, execution, trace, usage)
    return result


def _materialize_quality(value: object, linked: bool, trace: dict[str, Any]) -> dict[str, Any]:
    quality = dict(value) if isinstance(value, dict) else {}
    verdicts = [quality.get("standards_verdict"), quality.get("spec_verdict")]
    verification = quality.get("verification")
    if verification == "failed" or any(item == "MUST_FIX" for item in verdicts):
        gate_result = "fail"
    elif any(item == "DECISION_NEEDED" for item in verdicts):
        gate_result = "blocked"
    elif verification == "passed" or any(item == "APPROVE" for item in verdicts):
        gate_result = "pass"
    else:
        failed_gates = [
            item
            for item in trace.get("gates", [])
            if isinstance(item, dict) and item.get("status") in {"failed", "blocked"}
        ]
        gate_result = "fail" if failed_gates else "unknown"
    return {
        **quality,
        "gate_result": gate_result,
        "verify_passed": verification == "passed" if verification in {"passed", "failed"} else None,
        "evidence_complete": linked,
        "review_findings_count": quality.get("must_fix_rounds"),
    }


def _materialize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    numeric: dict[str, int] = {}
    for field in ("input_tokens", "output_tokens", "cache_write_tokens"):
        value = usage.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            numeric[field] = value
    cache_read = usage.get("cache_read_tokens", usage.get("cached_input_tokens"))
    if isinstance(cache_read, int) and not isinstance(cache_read, bool):
        numeric["cache_read_tokens"] = cache_read
    collected = bool(numeric)
    total = usage.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool):
        primary = [numeric[field] for field in ("input_tokens", "output_tokens") if field in numeric]
        total = sum(primary) if primary else (sum(numeric.values()) if collected else None)
    raw_models = usage.get("models") if isinstance(usage.get("models"), list) else []
    models = [item if isinstance(item, dict) else {"model": str(item)} for item in raw_models]
    return {
        **{
            field: numeric.get(field)
            for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        },
        "total_tokens": total,
        "model": usage.get("model") or "unknown",
        "models": models,
        "collection_status": "collected" if collected else "unavailable",
        "unavailable_reason": None if collected else "async_usage_unavailable",
        "source": "async_stage_supplement" if collected else "unknown",
        "attribution": "exact_session_window" if collected else "unknown",
        "usage_scope": "stage_session" if collected else "unknown",
        "contamination_risk": "low" if collected else "unknown",
        "confidence": "high" if collected else "unknown",
    }


def _materialize_collection(evidence: dict[str, Any]) -> dict[str, Any]:
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), dict) else {}
    summary = evidence.get("interaction_summary") if isinstance(evidence.get("interaction_summary"), dict) else {}
    return {
        "scope": "exact_session_window" if evidence else "none",
        "source_log_count": 1 if evidence else 0,
        "events_scanned": int(coverage.get("in_window", 0) or 0),
        "messages_scanned": int(summary.get("user_turns", 0) or 0) + int(summary.get("assistant_messages", 0) or 0),
        "messages_used": int(coverage.get("retained", 0) or 0),
        "user_turn_count": summary.get("user_turns"),
        "confidence": "high" if evidence else "unknown",
        "semantic_status": "supplement_collected" if evidence else "unavailable",
        "candidate_user_turn_count": int(summary.get("user_turns", 0) or 0),
        "classified_user_turn_count": 0,
        "malformed_event_count": 0,
    }


def _materialize_friction(evidence: dict[str, Any]) -> dict[str, Any]:
    tools = evidence.get("tools") if isinstance(evidence.get("tools"), dict) else {}
    failed = int(tools.get("failed", 0) or 0)
    return {
        "interventions": 0,
        "intervention_breakdown": {},
        "intervention_events": [],
        "rework_from_previous": False,
        "classification_source": "async_stage_supplement" if evidence else "unavailable",
        "confidence": "medium" if evidence else "unknown",
        "failed_tool_count": failed,
    }


def _evolution_evidence(
    evidence: dict[str, Any],
    execution: dict[str, Any],
    trace: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Keep only sanitized, bounded Supplement facts needed by evolution."""

    interactions = [item for item in evidence.get("interactions", []) if isinstance(item, dict)]
    bounded_interactions = interactions if len(interactions) <= 12 else [*interactions[:6], *interactions[-6:]]
    return {
        "interactions": [
            {
                "role": str(item.get("role") or "unknown")[:32],
                "text": str(item.get("text") or "")[:800],
            }
            for item in bounded_interactions
        ],
        "interaction_summary": dict(evidence.get("interaction_summary") or {}),
        "tools": dict(evidence.get("tools") or {}),
        "failed_tools": [
            {
                key: item.get(key)
                for key in ("name", "input_summary", "error_summary")
                if item.get(key) is not None
            }
            for item in evidence.get("failed_tools", [])[:20]
            if isinstance(item, dict)
        ],
        "coverage": dict(evidence.get("coverage") or {}),
        "execution": {
            key: execution.get(key)
            for key in ("tool_call_count", "subagent_spawn_count", "context_reads")
            if execution.get(key) is not None
        },
        "workflow_trace": {
            key: trace.get(key)
            for key in ("status", "route", "gates", "fallbacks", "outcome")
            if trace.get(key) is not None
        },
        "resource_usage": _bounded_evolution_usage(usage),
    }


def _bounded_evolution_usage(usage: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: usage.get(key)
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "model",
        )
        if usage.get(key) is not None
    }
    models = usage.get("models") if isinstance(usage.get("models"), list) else []
    if models:
        result["models"] = [
            {
                key: item.get(key)
                for key in (
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
                if item.get(key) is not None
            }
            for item in models[:20]
            if isinstance(item, dict)
        ]
    return result
