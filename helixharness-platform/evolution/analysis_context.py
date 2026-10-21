"""Build bounded, auditable model context from admitted telemetry facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from evolution.models import Capability, EvolutionEvidence

HISTORY_WINDOW_DAYS = 90
MAX_BREAKDOWN_VALUES = 50
MAX_PRIOR_HYPOTHESES = 50
MAX_PRIOR_EXPERIMENTS = 50


@dataclass(frozen=True)
class AnalysisContextSnapshot:
    """One immutable payload sent to the semantic analyzer for a capability."""

    analysis_context_id: str
    cycle_id: str
    capability_id: str
    source_digest: str
    payload: dict[str, Any]
    payload_hash: str
    trigger_count: int
    context_count: int


def build_analysis_contexts(
    evidence: Sequence[EvolutionEvidence],
    focus_evidence_ids: set[str],
    capabilities: Sequence[Capability],
    *,
    cycle_id: str,
    source_digest: str,
    as_of: datetime,
    prior_hypotheses: Sequence[Mapping[str, Any]] = (),
    prior_experiments: Sequence[Mapping[str, Any]] = (),
    history_window_days: int = HISTORY_WINDOW_DAYS,
) -> tuple[AnalysisContextSnapshot, ...]:
    """Freeze full incremental facts plus time-bounded, whole-population summaries."""

    as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    as_of_utc = as_of_utc.astimezone(timezone.utc)
    snapshots: list[AnalysisContextSnapshot] = []
    latest_increment = tuple(
        item for item in evidence if item.evidence_id in focus_evidence_ids
    )
    history = tuple(item for item in evidence if item.evidence_id not in focus_evidence_ids)
    global_history_summary = _history_summary(
        history,
        as_of=as_of_utc,
        window_days=history_window_days,
    )
    for capability in capabilities:
        cluster = tuple(item for item in evidence if _matches(capability, item))
        triggers = tuple(item for item in cluster if item.evidence_id in focus_evidence_ids)
        if not triggers:
            continue
        capability_history = tuple(
            item
            for item in history
            if _matches(capability, item)
        )
        capability_history_summary = _history_summary(
            capability_history,
            as_of=as_of_utc,
            window_days=history_window_days,
        )
        context_evidence_ids = tuple(item.evidence_id for item in latest_increment)
        payload = {
            "schema_version": "evolution-analysis-context.v2",
            "as_of": _timestamp(as_of_utc),
            "source_digest": source_digest,
            "capability": {
                "id": capability.capability_id,
                "name": capability.name,
                "description": capability.description,
            },
            "scope": {
                "qualified_evidence_count": len(evidence),
                "capability_evidence_count": len(cluster),
                "latest_increment_count": len(latest_increment),
                "capability_trigger_count": len(triggers),
                "history_window_days": history_window_days,
                "history_evidence_count": global_history_summary["in_window_count"],
                "capability_history_evidence_count": capability_history_summary[
                    "in_window_count"
                ],
            },
            "latest_increment": [
                _evidence_payload(item, "NEW_OR_CHANGED")
                for item in latest_increment
            ],
            "capability_trigger_evidence": [
                _evidence_payload(item, "NEW_OR_CHANGED") for item in triggers
            ],
            "history_summary": global_history_summary,
            "capability_history_summary": capability_history_summary,
            "prior_hypotheses": _prior_hypothesis_payloads(
                capability.capability_id,
                prior_hypotheses,
            )[:MAX_PRIOR_HYPOTHESES],
            "prior_experiments": _prior_experiment_payloads(
                capability.capability_id,
                prior_experiments,
            )[:MAX_PRIOR_EXPERIMENTS],
            "context_evidence_ids": list(context_evidence_ids),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        context_id = "ctx_" + hashlib.sha256(
            f"{cycle_id}:{capability.capability_id}:{payload_hash}".encode("utf-8")
        ).hexdigest()[:20]
        snapshots.append(
            AnalysisContextSnapshot(
                analysis_context_id=context_id,
                cycle_id=cycle_id,
                capability_id=capability.capability_id,
                source_digest=source_digest,
                payload=payload,
                payload_hash=payload_hash,
                trigger_count=len(latest_increment),
                context_count=(
                    len(latest_increment)
                    + int(global_history_summary["in_window_count"])
                ),
            )
        )
    return tuple(snapshots)


def _matches(capability: Capability, evidence: EvolutionEvidence) -> bool:
    searchable = " ".join(
        value
        for value in (
            evidence.command,
            evidence.stage_kind,
            evidence.outcome_reason,
            evidence.feature,
        )
        if value
    ).lower()
    return any(selector.lower() in searchable for selector in capability.selectors)


def _evidence_payload(item: EvolutionEvidence, scope: str) -> dict[str, Any]:
    payload = asdict(item)
    payload["analysis_scope"] = scope
    return payload


def _history_summary(
    evidence: Sequence[EvolutionEvidence],
    *,
    as_of: datetime,
    window_days: int,
) -> dict[str, Any]:
    window_start = as_of - timedelta(days=window_days)
    in_window: list[tuple[EvolutionEvidence, datetime]] = []
    older_count = 0
    future_count = 0
    undated_count = 0
    for item in evidence:
        occurred_at = _parse_timestamp(item.occurred_at or item.admitted_at)
        if occurred_at is None:
            undated_count += 1
        elif occurred_at > as_of:
            future_count += 1
        elif occurred_at < window_start:
            older_count += 1
        else:
            in_window.append((item, occurred_at))
    items = tuple(item for item, _occurred_at in in_window)
    return {
        "window_days": window_days,
        "window_start": _timestamp(window_start),
        "window_end": _timestamp(as_of),
        "available_history_count": len(evidence),
        "in_window_count": len(items),
        "excluded_older_count": older_count,
        "excluded_future_count": future_count,
        "undated_count": undated_count,
        "project_count": len({item.project_name for item in items if item.project_name}),
        "complete_evidence_count": sum(item.evidence_complete is True for item in items),
        "incomplete_evidence_count": sum(item.evidence_complete is False for item in items),
        "stage_breakdown": _breakdown(item.stage_kind for item in items),
        "status_breakdown": _breakdown(item.status for item in items),
        "gate_result_breakdown": _breakdown(item.gate_result for item in items),
        "outcome_reason_breakdown": _breakdown(item.outcome_reason for item in items),
        "command_breakdown": _breakdown(item.command for item in items),
        "project_breakdown": _breakdown(item.project_name for item in items),
        "weekly_trend": _weekly_trend(in_window),
    }


def _breakdown(values: Iterable[str | None]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    kept = ranked[:MAX_BREAKDOWN_VALUES]
    return {
        "values": [
            {
                "value": value,
                "count": count,
            }
            for value, count in kept
        ],
        "other_value_count": max(0, len(ranked) - len(kept)),
        "other_evidence_count": sum(count for _value, count in ranked[len(kept) :]),
    }


def _weekly_trend(
    evidence: Sequence[tuple[EvolutionEvidence, datetime]],
) -> list[dict[str, Any]]:
    buckets: dict[str, list[EvolutionEvidence]] = {}
    for item, occurred_at in evidence:
        week_start = (occurred_at - timedelta(days=occurred_at.weekday())).date().isoformat()
        buckets.setdefault(week_start, []).append(item)
    return [
        {
            "week_start": week_start,
            "evidence_count": len(items),
            "project_count": len({item.project_name for item in items if item.project_name}),
            "blocked_or_partial_count": sum(
                item.status in {"blocked", "partial"} for item in items
            ),
            "failed_gate_count": sum(
                (item.gate_result or "").lower()
                in {"fail", "failed", "reject", "rejected"}
                for item in items
            ),
            "incomplete_evidence_count": sum(
                item.evidence_complete is False for item in items
            ),
        }
        for week_start, items in sorted(buckets.items())
    ]


def _prior_hypothesis_payloads(
    capability_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = _decode_json_mapping(row.get("payload_json"))
        if str(payload.get("capability_id") or row.get("capability_id") or "") != capability_id:
            continue
        payloads.append(
            {
                "hypothesis_id": payload.get("hypothesis_id") or row.get("hypothesis_id"),
                "kind": payload.get("kind") or row.get("kind"),
                "title": payload.get("title") or row.get("title"),
                "rationale": payload.get("rationale"),
                "evidence_ids": payload.get("evidence_ids") or [],
                "analysis_context_id": payload.get("analysis_context_id"),
                "selected": bool(row.get("selected_at")),
                "ignored": bool(row.get("ignored_at")),
                "deleted": bool(row.get("deleted_at")),
            }
        )
    return payloads


def _prior_experiment_payloads(
    capability_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("capability_id") or "") != capability_id:
            continue
        payloads.append(
            {
                "experiment_id": row.get("experiment_id"),
                "hypothesis_id": row.get("hypothesis_id"),
                "state": row.get("state"),
                "decision_reason": row.get("decision_reason"),
                "benchmark_run_id": row.get("benchmark_run_id"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return payloads


def _decode_json_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    tick = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return tick.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
