"""Bounded daily AI context; SQLite stores projections, never telemetry originals.

Changes are dated by successful observation in Shanghai, not execution time.
The generation callback runs inside one transaction so a failed model request
cannot consume an increment or replace a successful daily summary.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

try:
    import pysqlite3 as sqlite3
except ImportError:  # pragma: no cover
    import sqlite3

CONTRACT = "daily-facts-v1"
HISTORY_FILE = "ai_daily_history.db"
MAX_INPUT_CHARS = 20_000
RECENT_DAYS = 7
BUSINESS_STAGES = {
    "requirement": "需求与计划",
    "coding": "开发与验证",
    "archive": "归档",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identifier(value: Any) -> str | None:
    """Accept technical identifiers, excluding messages, shell commands and URLs."""
    text = str(value or "")
    return text if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.:-]{0,95}", text) else None


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def daily_records(features: list[dict]) -> list[dict]:
    """Project stable, allowlisted facts for every project and physical entry."""
    from eval.usage_loader import _duration_sec, _resource_usage, _workflow

    records = []
    for feature in features:
        project_key = _hash([feature.get("project_id"), feature.get("feature")])
        identity = {
            "project_key": project_key,
            "project": str(feature.get("project_name") or "")[:120],
            "feature": str(feature.get("feature") or "")[:120],
        }
        stages = [s for s in feature.get("stage_timeline", []) if isinstance(s, dict)]
        records.append({
            **identity, "record_key": project_key, "kind": "project",
            "archive_completed": bool(feature.get("archive_completed")),
            "collection_coverage": _identifier(feature.get("collection_coverage")),
            "expected_stage_count": _number(feature.get("expected_stage_count")),
            "observed_stage_count": sum(bool(s.get("entry_count")) for s in stages),
            "current_stage": BUSINESS_STAGES.get(feature.get("current_stage"), "未知阶段"),
            "stages": [
                {
                    "stage": BUSINESS_STAGES.get(s.get("stage_kind"), "其他阶段"),
                    "status": _identifier(s.get("status")),
                    "entries": _number(s.get("entry_count")),
                    "attempts": _number(s.get("attempt_count")),
                    "continuations": _number(s.get("continuation_segment_count")),
                }
                for s in stages if s.get("entry_count")
            ],
        })
        for entry in feature.get("entries", []):
            entry_id = entry.get("entry_id")
            if not entry_id:
                raise ValueError("daily insight requires a stable entry_id")
            usage = _resource_usage(entry)
            workflow = _workflow(entry)
            async_data = entry.get("async_evidence") or {}
            linked = entry.get("telemetry_status") == "complete"
            trace = (async_data.get("workflow_trace") or {}) if linked else {}
            gates = trace.get("gates") or []
            failed_tools = (async_data.get("failed_tools") or []) if linked else []
            records.append({
                **identity, "record_key": _hash([project_key, entry_id]), "kind": "entry",
                "stage": BUSINESS_STAGES.get(entry.get("stage_kind"), "其他阶段"),
                "command": _identifier(entry.get("command")),
                "status": _identifier(entry.get("status")),
                "failure_code": _identifier(entry.get("failure_code")),
                "outcome_reason": _identifier(entry.get("outcome_reason") or entry.get("end_reason")),
                "duration_sec": _duration_sec(entry),
                "resource_usage": {
                    key: usage.get(key) for key in (
                        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                        "total_tokens", "collection_status",
                    )
                },
                "telemetry_status": _identifier(entry.get("telemetry_status")),
                "async_linked": linked,
                "repeated": bool(workflow.get("stage_repeated")),
                "continuation": bool(workflow.get("continuation_of_entry_id")),
                "failed_gate_count": sum(isinstance(g, dict) and g.get("status") in {"failed", "blocked"} for g in gates) if linked and isinstance(trace.get("gates"), list) else None,
                "failed_gate_names": sorted({
                    name for gate in gates if isinstance(gate, dict) and gate.get("status") in {"failed", "blocked"}
                    if (name := _identifier(gate.get("gate") or gate.get("name") or gate.get("id")))
                })[:12],
                "failed_tool_count": len(failed_tools) if linked and isinstance(async_data.get("failed_tools"), list) else None,
                "failed_tool_names": sorted({
                    name for tool in failed_tools
                    if isinstance(tool, dict)
                    if (name := _identifier(tool.get("tool") or tool.get("name")))
                })[:12],
                "fallback_count": len(trace.get("fallbacks") or []) if linked and isinstance(trace.get("fallbacks"), list) else None,
            })
    return sorted(records, key=lambda r: r["record_key"])


def global_facts(dataset: dict) -> dict:
    """Whole-population denominators, stage distribution and separate issue counts."""
    records = dataset.get("daily_records", [])
    projects = [r for r in records if r["kind"] == "project"]
    entries = [r for r in records if r["kind"] == "entry"]
    issues: dict[str, set] = defaultdict(set)
    stages: dict[str, dict] = {}
    for record in projects:
        key = record["project_key"]
        if record.get("collection_coverage") == "partial":
            issues["incomplete_collection"].add(key)
        for stage in record.get("stages", []):
            if stage.get("status") in {"failed", "blocked", "partial"}:
                issues["latest_stage_exception"].add(key)
            if (stage.get("attempts") or 0) > 1:
                issues["repeated_execution"].add(key)
            if stage.get("continuations"):
                issues["cross_session_continuation"].add(key)
    for record in entries:
        key = record["project_key"]
        for label, present in {
            "recorded_failed": record.get("status") == "failed",
            "recorded_blocked": record.get("status") == "blocked",
            "recorded_partial": record.get("status") == "partial",
            "failed_gate": bool(record.get("failed_gate_count")),
            "failed_tool": bool(record.get("failed_tool_count")),
            "fallback": bool(record.get("fallback_count")),
            "async_pending": record.get("telemetry_status") in {"pending", "partial"},
        }.items():
            if present:
                issues[label].add(key)
        stage = stages.setdefault(record["stage"], {
            "entry_count": 0, "projects": set(), "duration_samples": 0, "duration_sec": 0,
        })
        stage["entry_count"] += 1
        stage["projects"].add(key)
        if record.get("duration_sec") is not None:
            stage["duration_samples"] += 1
            stage["duration_sec"] += record["duration_sec"]
    for stage in stages.values():
        stage["project_count"] = len(stage.pop("projects"))
        if not stage["duration_samples"]:
            stage["duration_sec"] = None
    raw = dataset.get("summary") or {}
    summary = {key: raw.get(key) for key in (
        "project_total", "repository_total", "user_total", "archived_projects", "total_duration_sec",
        "avg_duration_sec", "avg_duration_sample_projects", "avg_duration_missing_projects", "async_pending_entries",
    )}
    summary["avg_duration_scope"] = "已归档且有耗时记录的项目；耗时为累计阶段执行时间"
    duration_samples = [r["duration_sec"] for r in entries if r.get("duration_sec") is not None]
    summary["total_duration_sec"] = sum(duration_samples) if duration_samples else None
    summary["duration_sample_entries"] = len(duration_samples)
    summary["resource_usage"] = {key: (raw.get("resource_usage") or {}).get(key) for key in (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens",
        "eligible_entries", "collected_entries", "missing_entries", "exact", "exact_total_tokens",
    )}
    summary["code_delta"] = {key: (raw.get("code_delta") or {}).get(key) for key in (
        "files_changed", "lines_added", "lines_deleted", "collection_status",
    )}
    return {
        "summary": summary,
        "stage_distribution": stages,
        "issue_projects": dict(sorted((label, len(keys)) for label, keys in issues.items())),
        "issue_denominator_projects": len(projects),
        "entry_count": len(entries),
        "evidence_coverage": {
            "eligible_entries": len(entries),
            "async_linked_entries": sum(bool(r.get("async_linked")) for r in entries),
            "gate_observed_entries": sum(r.get("failed_gate_count") is not None for r in entries),
            "tool_observed_entries": sum(r.get("failed_tool_count") is not None for r in entries),
            "fallback_observed_entries": sum(r.get("fallback_count") is not None for r in entries),
        },
    }


def _history(connection: sqlite3.Connection, scope: str, day: str) -> dict:
    rows = connection.execute(
        "SELECT day, answer, facts FROM daily_summaries WHERE scope=? AND day<? ORDER BY day", (scope, day),
    ).fetchall()
    recent, older = rows[-RECENT_DAYS:], rows[:-RECENT_DAYS]
    result: dict[str, Any] = {
        "recent_generated_days": [{"day": d, "summary": answer} for d, answer, _ in recent],
        "generated_day_count": len(rows),
    }
    if older:
        counts: Counter = Counter()
        for _, _, facts in older:
            counts.update(label for label, count in json.loads(facts).get("issue_projects", {}).items() if count)
        result["older_history"] = {
            "from": older[0][0], "through": older[-1][0], "generated_days": len(older),
            "days_with_issue": dict(counts),
            "last_snapshot": json.loads(older[-1][2]),
            "last_summary": older[-1][1],
            "scope": "历史生成日快照；日期可能不连续，次数是出现该类记录的天数，不是新增项目或未解决问题数",
        }
    return result


def _batches(items: list[dict], limit: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 2
    for item in items:
        item_size = len(_json(item)) + 1
        if item_size + 2 > limit:
            raise ValueError("daily fact exceeds batch budget")
        if current and size + item_size > limit:
            batches.append(current)
            current, size = [], 2
        current.append(item)
        size += item_size
    if current:
        batches.append(current)
    return batches


def bounded_payload(base: dict, changes: list[dict], summarize: Callable[[dict], str], limit: int) -> dict:
    """Every record reaches a model call; recursively reduce oversized increments."""
    payload = {**base, "changes": changes}
    if len(_json(payload)) <= limit:
        return payload
    allowance = limit - len(_json(base)) - 300
    if allowance < 2000:
        raise ValueError("daily context budget too small for global facts and history")
    items = changes
    rounds = 0
    while len(_json(items)) > allowance:
        rounds += 1
        reduced = []
        for batch in _batches(items, min(allowance, limit - 1000)):
            text = summarize({"records_or_summaries": batch, "reduction_round": rounds})
            if not str(text or "").strip():
                raise ValueError("empty daily batch summary")
            reduced.append({"summary": str(text)[:900]})
        if len(_json(reduced)) >= len(_json(items)):
            raise ValueError("daily batch summaries did not reduce input size")
        items = reduced
    payload = {**base, "change_batch_summaries": items, "reduction_rounds": rounds}
    if len(_json(payload)) > limit:
        raise ValueError("daily context exceeds input budget")
    return payload


def generate_daily_insight(
    dataset: dict, cache_dir: Path, roots: list[Path],
    generate: Callable[[dict], dict], summarize: Callable[[dict], str],
    *, now: datetime | None = None, input_limit: int = MAX_INPUT_CHARS,
) -> dict:
    """Generate once against current facts and atomically save a daily revision."""
    observed_at = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    timestamp, day = observed_at.isoformat(timespec="seconds"), observed_at.date().isoformat()
    scope = _hash([CONTRACT, sorted(str(r.resolve()) for r in roots)])
    cache_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(cache_dir / HISTORY_FILE, timeout=1)
    try:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                scope TEXT, record_key TEXT, payload TEXT, PRIMARY KEY(scope, record_key));
            CREATE TABLE IF NOT EXISTS daily_changes (
                scope TEXT, day TEXT, record_key TEXT, payload TEXT, PRIMARY KEY(scope, day, record_key));
            CREATE TABLE IF NOT EXISTS daily_summaries (
                scope TEXT, day TEXT, answer TEXT, facts TEXT, metadata TEXT, PRIMARY KEY(scope, day));
            CREATE TABLE IF NOT EXISTS checkpoints (scope TEXT PRIMARY KEY, observed_at TEXT);
        """)
        connection.execute("BEGIN IMMEDIATE")
        previous = dict(connection.execute("SELECT record_key, payload FROM observations WHERE scope=?", (scope,)))
        checkpoint = connection.execute("SELECT observed_at FROM checkpoints WHERE scope=?", (scope,)).fetchone()
        same_day = connection.execute(
            "SELECT metadata FROM daily_summaries WHERE scope=? AND day=?", (scope, day),
        ).fetchone()
        metadata = json.loads(same_day[0]) if same_day else {
            "day": day, "timezone": "Asia/Shanghai", "bootstrap": checkpoint is None,
            "observed_since": checkpoint[0] if checkpoint else None,
        }
        metadata["observed_through"] = timestamp
        changes = {key: json.loads(value) for key, value in connection.execute(
            "SELECT record_key, payload FROM daily_changes WHERE scope=? AND day=?", (scope, day),
        )}
        current = {r["record_key"]: _json(r) for r in dataset.get("daily_records", [])}
        for key, value in current.items():
            if previous.get(key) != value:
                prior_change = changes.get(key, {}).get("change")
                change = (
                    "reappeared_in_scope" if prior_change == "removed_from_scope"
                    else prior_change or ("bootstrap" if not checkpoint else "updated" if key in previous else "new")
                )
                changes[key] = {**json.loads(value), "change": change}
        for key in previous.keys() - current.keys():
            old = json.loads(previous[key])
            changes[key] = {key_: old[key_] for key_ in ("record_key", "project_key", "project", "feature", "kind")}
            changes[key]["change"] = "removed_from_scope"
        metadata["record_count"] = len(changes)
        metadata["project_count"] = len({r["project_key"] for r in changes.values()})
        metadata["change_counts"] = dict(Counter(r["change"] for r in changes.values()))
        facts = global_facts(dataset)
        base = {
            **facts, "contract": CONTRACT, "daily_window": metadata,
            "history": _history(connection, scope, day),
        }
        payload = bounded_payload(base, [changes[key] for key in sorted(changes)], summarize, input_limit)
        result = generate(payload)
        if result.get("status") != "success" or not str(result.get("answer") or "").strip():
            raise ValueError("daily insight did not return a successful nonempty answer")
        connection.execute("DELETE FROM observations WHERE scope=?", (scope,))
        connection.executemany("INSERT INTO observations VALUES (?, ?, ?)", [(scope, key, value) for key, value in current.items()])
        connection.executemany("INSERT OR REPLACE INTO daily_changes VALUES (?, ?, ?, ?)", [
            (scope, day, key, _json(value)) for key, value in changes.items()
        ])
        connection.execute("INSERT OR REPLACE INTO daily_summaries VALUES (?, ?, ?, ?, ?)", (
            scope, day, result["answer"][:1200], _json(facts), _json(metadata),
        ))
        connection.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?)", (scope, timestamp))
        connection.commit()
        result["daily_context"] = {
            **metadata, "input_chars": len(_json(payload)), "input_limit_chars": input_limit,
            "reduction_rounds": payload.get("reduction_rounds", 0),
        }
        return result
    finally:
        connection.close()
