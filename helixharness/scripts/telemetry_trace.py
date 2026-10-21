#!/usr/bin/env python3
"""Private, bounded workflow facts for Lite evolution hypotheses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

TRACE_SCHEMA = "helixharness-workflow-trace/1.0"
PRIVATE_TRACE_ROOT = Path(".helixharness") / "telemetry-v2" / "traces"
ENTRY_ID = re.compile(r"^entry-[0-9a-f]{16}$")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
COMMANDS = {"propose", "apply", "archive"}
EVENT_KINDS = {"gate", "fallback"}
GATE_STATUSES = {"passed", "failed", "blocked", "skipped"}
TERMINAL_STATUSES = {"completed", "failed", "blocked", "partial"}
MAX_EVENTS = 256


class WorkflowTraceError(ValueError):
    """Raised when a trace would contain unbounded or conflicting facts."""


def validate_identifier(value: Any) -> str:
    """Validate one caller-provided controlled identifier."""
    return str(_identifier(value))


def _identifier(value: Any, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    normalized = str(value or "").strip()
    if not IDENTIFIER.fullmatch(normalized):
        raise WorkflowTraceError("invalid workflow trace identifier")
    return normalized


def _trace_path(repo: Path, entry_id: str) -> Path:
    if not ENTRY_ID.fullmatch(str(entry_id or "")):
        raise WorkflowTraceError("invalid workflow trace entry id")
    root = Path(repo).expanduser().resolve()
    relative = PRIVATE_TRACE_ROOT / f"{entry_id}.json"
    current = root
    for part in relative.parts:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise WorkflowTraceError("workflow trace path contains symlink")
    return root / relative


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowTraceError("workflow trace unavailable") from exc
    if not isinstance(value, dict) or value.get("schema") != TRACE_SCHEMA:
        raise WorkflowTraceError("invalid workflow trace")
    return value


def _exact_keys(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowTraceError("invalid workflow trace object")
    optional = optional or set()
    if not required.issubset(value) or set(value) - required - optional:
        raise WorkflowTraceError("invalid workflow trace fields")
    return value


def _validated_event(event: Any, field: str) -> dict[str, Any]:
    common = {"sequence", "event_id"}
    if field == "transitions":
        value = _exact_keys(event, common | {"from", "to", "reason_code"})
        for key in ("event_id", "from", "to", "reason_code"):
            _identifier(value[key])
    elif field == "gates":
        value = _exact_keys(
            event,
            common | {"check_id", "status"},
            {"reason_code", "result_code"},
        )
        if value.get("event_id") != value.get("check_id") or value.get("status") not in GATE_STATUSES:
            raise WorkflowTraceError("invalid workflow trace gate")
        for key in ("event_id", "check_id", "reason_code", "result_code"):
            if key in value:
                _identifier(value[key])
    else:
        value = _exact_keys(
            event,
            common | {"fallback_id", "taken"},
            {"reason_code"},
        )
        if value.get("event_id") != value.get("fallback_id") or not isinstance(value.get("taken"), bool):
            raise WorkflowTraceError("invalid workflow trace fallback")
        for key in ("event_id", "fallback_id", "reason_code"):
            if key in value:
                _identifier(value[key])
    sequence = value.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise WorkflowTraceError("invalid workflow trace sequence")
    return value


def _validated_for_upload(trace: dict[str, Any], expected_entry_id: str) -> dict[str, Any]:
    value = _exact_keys(
        trace,
        {
            "schema",
            "status",
            "entry_id",
            "component_versions",
            "route",
            "transitions",
            "gates",
            "fallbacks",
            "outcome",
        },
    )
    if value.get("schema") != TRACE_SCHEMA or value.get("status") not in {"collecting", "collected"}:
        raise WorkflowTraceError("invalid workflow trace status")
    if value.get("entry_id") != expected_entry_id or not ENTRY_ID.fullmatch(expected_entry_id):
        raise WorkflowTraceError("invalid workflow trace entry id")

    versions = _exact_keys(value.get("component_versions"), {"helixharness_version", "components"})
    if not VERSION.fullmatch(str(versions.get("helixharness_version") or "")):
        raise WorkflowTraceError("invalid workflow trace version")
    route = _exact_keys(
        value.get("route"),
        {"action", "command", "decision_code", "workflow_path"},
    )
    command = str(route.get("command") or "")
    expected_workflow = str(route.get("workflow_path") or "")
    if (
        route.get("action") != "load_workflow"
        or command not in COMMANDS
        or expected_workflow != f"workflows/single/{command}.md"
    ):
        raise WorkflowTraceError("invalid workflow trace route")
    _identifier(route.get("decision_code"))

    components = _exact_keys(versions.get("components"), {"router", "workflow", "recorder"})
    expected_paths = {
        "router": "scripts/workflow_router.py",
        "workflow": expected_workflow,
        "recorder": "scripts/metrics.py",
    }
    for name, expected_path in expected_paths.items():
        component = _exact_keys(components.get(name), {"path", "sha256"})
        if component.get("path") != expected_path or not SHA256.fullmatch(str(component.get("sha256") or "")):
            raise WorkflowTraceError("invalid workflow trace component")

    sequences: list[int] = []
    event_ids: list[str] = []
    total = 0
    for field in ("transitions", "gates", "fallbacks"):
        events = value.get(field)
        if not isinstance(events, list):
            raise WorkflowTraceError("invalid workflow trace event list")
        total += len(events)
        validated = [_validated_event(event, field) for event in events]
        sequences.extend(event["sequence"] for event in validated)
        event_ids.extend(str(event["event_id"]) for event in validated)
    if (
        total > MAX_EVENTS
        or sorted(sequences) != list(range(1, total + 1))
        or len(event_ids) != len(set(event_ids))
    ):
        raise WorkflowTraceError("invalid workflow trace event sequence")

    transitions = value["transitions"]
    if not transitions or transitions[0].get("event_id") != "stage_started":
        raise WorkflowTraceError("invalid workflow trace start transition")

    outcome = value.get("outcome")
    if value["status"] == "collecting":
        if outcome is not None or any(event.get("event_id") == "stage_terminal" for event in transitions):
            raise WorkflowTraceError("invalid collecting workflow trace")
    else:
        outcome_value = _exact_keys(outcome, {"status", "reason_code"})
        if outcome_value.get("status") not in TERMINAL_STATUSES:
            raise WorkflowTraceError("invalid workflow trace outcome")
        _identifier(outcome_value.get("reason_code"))
        terminal = transitions[-1]
        if (
            terminal.get("event_id") != "stage_terminal"
            or terminal.get("to") != outcome_value.get("status")
            or terminal.get("reason_code") != outcome_value.get("reason_code")
        ):
            raise WorkflowTraceError("invalid workflow trace terminal transition")
    return value


def _component(path: Path, relative: str) -> dict[str, str]:
    candidate = path / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise WorkflowTraceError(f"workflow trace component unavailable: {relative}")
    return {
        "path": relative,
        "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
    }


def _next_sequence(trace: dict[str, Any]) -> int:
    events = [
        event
        for field in ("transitions", "gates", "fallbacks")
        for event in trace.get(field, [])
        if isinstance(event, dict)
    ]
    return 1 + max((int(event.get("sequence") or 0) for event in events), default=0)


def _append(trace: dict[str, Any], field: str, event: dict[str, Any]) -> None:
    events = trace.get(field)
    if not isinstance(events, list):
        raise WorkflowTraceError("invalid workflow trace event list")
    event_id = str(event["event_id"])
    for existing_field in ("transitions", "gates", "fallbacks"):
        for existing in trace.get(existing_field, []):
            if not isinstance(existing, dict) or existing.get("event_id") != event_id:
                continue
            comparable = {key: value for key, value in existing.items() if key != "sequence"}
            if existing_field == field and comparable == event:
                return
            raise WorkflowTraceError("workflow trace event id conflict")
    total = sum(len(trace.get(name, [])) for name in ("transitions", "gates", "fallbacks"))
    if total >= MAX_EVENTS:
        raise WorkflowTraceError("workflow trace event limit exceeded")
    events.append({"sequence": _next_sequence(trace), **event})


def start_trace(
    repo: Path,
    *,
    entry_id: str,
    command: str,
    route_reason: str | None,
    plugin_root: Path,
    helixharness_version: str,
) -> dict[str, Any]:
    if command not in COMMANDS:
        raise WorkflowTraceError("unsupported workflow trace command")
    decision_code = _identifier(route_reason or "unavailable")
    root = Path(plugin_root).expanduser().resolve()
    workflow_path = f"workflows/single/{command}.md"
    trace = {
        "schema": TRACE_SCHEMA,
        "status": "collecting",
        "entry_id": str(entry_id),
        "component_versions": {
            "helixharness_version": str(helixharness_version),
            "components": {
                "router": _component(root, "scripts/workflow_router.py"),
                "workflow": _component(root, workflow_path),
                "recorder": _component(root, "scripts/metrics.py"),
            },
        },
        "route": {
            "action": "load_workflow",
            "command": command,
            "decision_code": decision_code,
            "workflow_path": workflow_path,
        },
        "transitions": [],
        "gates": [],
        "fallbacks": [],
        "outcome": None,
    }
    _append(
        trace,
        "transitions",
        {
            "event_id": "stage_started",
            "from": "not_started",
            "to": "in_progress",
            "reason_code": "stage_started",
        },
    )
    path = _trace_path(repo, entry_id)
    if path.exists():
        existing = _load(path)
        if existing != trace:
            raise WorkflowTraceError("existing workflow trace does not match stage")
        return existing
    _atomic_write(path, trace)
    return trace


def record_event(
    repo: Path,
    entry_id: str,
    *,
    event_id: str,
    kind: str,
    status: str | None = None,
    reason_code: str | None = None,
    taken: bool | None = None,
) -> dict[str, Any]:
    path = _trace_path(repo, entry_id)
    trace = _load(path)
    if trace.get("status") != "collecting":
        raise WorkflowTraceError("workflow trace is already terminal")
    normalized_event_id = _identifier(event_id)
    normalized_reason = _identifier(reason_code, allow_none=True)
    if kind not in EVENT_KINDS:
        raise WorkflowTraceError("unsupported workflow trace event kind")
    if kind == "gate":
        if status not in GATE_STATUSES or taken is not None:
            raise WorkflowTraceError("invalid workflow trace gate")
        event = {
            "event_id": normalized_event_id,
            "check_id": normalized_event_id,
            "status": status,
            **({"reason_code": normalized_reason} if normalized_reason else {}),
        }
        field = "gates"
    else:
        if not isinstance(taken, bool) or status is not None:
            raise WorkflowTraceError("invalid workflow trace fallback")
        event = {
            "event_id": normalized_event_id,
            "fallback_id": normalized_event_id,
            "taken": taken,
            **({"reason_code": normalized_reason} if normalized_reason else {}),
        }
        field = "fallbacks"
    _append(trace, field, event)
    _atomic_write(path, trace)
    return trace


def finish_trace(
    repo: Path,
    entry_id: str,
    *,
    status: str,
    failure_code: str | None,
    quality: dict[str, Any],
    recovery: str | None = None,
) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise WorkflowTraceError("invalid workflow trace terminal status")
    path = _trace_path(repo, entry_id)
    trace = _load(path)
    quality_gates = (
        ("verification", quality.get("verification"), {"passed": "passed", "failed": "failed", "not_run": "skipped"}),
        ("standards_review", quality.get("standards_verdict"), {"APPROVE": "passed", "MUST_FIX": "failed", "DECISION_NEEDED": "blocked"}),
        ("spec_review", quality.get("spec_verdict"), {"APPROVE": "passed", "MUST_FIX": "failed", "DECISION_NEEDED": "blocked"}),
    )
    for check_id, value, mapping in quality_gates:
        if value in mapping:
            _append(
                trace,
                "gates",
                {
                    "event_id": check_id,
                    "check_id": check_id,
                    "status": mapping[value],
                    "result_code": str(value).lower(),
                },
            )
    if recovery:
        recovery_id = _identifier(recovery)
        _append(
            trace,
            "fallbacks",
            {
                "event_id": recovery_id,
                "fallback_id": recovery_id,
                "taken": True,
            },
        )
    reason = _identifier(failure_code or "workflow_completed")
    _append(
        trace,
        "transitions",
        {
            "event_id": "stage_terminal",
            "from": "in_progress",
            "to": status,
            "reason_code": reason,
        },
    )
    trace["status"] = "collected"
    trace["outcome"] = {
        "status": status,
        "reason_code": reason,
    }
    _atomic_write(path, trace)
    return trace


def load_for_supplement(repo: Path, entry_id: str) -> dict[str, Any]:
    try:
        trace = _validated_for_upload(_load(_trace_path(repo, entry_id)), entry_id)
    except WorkflowTraceError:
        return {
            "schema": TRACE_SCHEMA,
            "status": "unavailable",
            "reason_code": "trace_unavailable",
        }
    return trace
