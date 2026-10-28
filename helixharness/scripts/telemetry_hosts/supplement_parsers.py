"""Built-in exact-window parsers for Lite telemetry host adapters.

The collector consumes only the normalized ``SupplementWindow`` interface.
Host log formats stay local to adapters so adding a future host does not change
the Ledger or collector control flow.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import SupplementWindow


MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 200_000


def _message_text(payload: dict[str, Any]) -> str:
    for field in ("message", "text", "content"):
        value = payload.get(field)
        if isinstance(value, str):
            return value
    return ""


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _in_window(value: Any, started_at: str, ended_at: str) -> bool:
    timestamp = _timestamp(value)
    start = _timestamp(started_at)
    end = _timestamp(ended_at)
    return (
        timestamp is not None
        and start is not None
        and end is not None
        and start <= timestamp <= end
    )


def _source_lines(path: Path, max_bytes: int | None) -> list[str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("session log unavailable or too large")
    size = path.stat().st_size
    limit = size if max_bytes is None else max_bytes
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 0
        or limit > size
        or limit > MAX_LOG_BYTES
    ):
        raise ValueError("session log unavailable or too large")
    with path.open("rb") as handle:
        payload = handle.read(limit)
    if len(payload) != limit:
        raise ValueError("session log changed before frozen snapshot was read")
    return payload.decode("utf-8").splitlines()


def _mark_last_assistant_final(events: list[dict[str, Any]]) -> None:
    for event in reversed(events):
        if event.get("type") == "assistant":
            event["final"] = True
            return


def collect_codex_window(
    path: Path,
    *,
    started_at: str,
    ended_at: str,
    max_bytes: int | None = None,
) -> SupplementWindow:
    events: list[dict[str, Any]] = []
    baseline_usage: dict[str, int] | None = None
    current_usage: dict[str, int] | None = None
    model: str | None = None
    calls: dict[str, dict[str, Any]] = {}
    for ordinal, line in enumerate(_source_lines(path, max_bytes)):
        if ordinal >= MAX_EVENTS:
            raise ValueError("session log event limit exceeded")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        timestamp = raw.get("timestamp")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        if payload_type == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            totals = (
                info.get("total_token_usage")
                if isinstance(info.get("total_token_usage"), dict)
                else info
            )
            normalized = {
                field: value
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
                if isinstance((value := totals.get(field)), int)
                and not isinstance(value, bool)
            }
            parsed = _timestamp(timestamp)
            start = _timestamp(started_at)
            if parsed is not None and start is not None and parsed < start:
                baseline_usage = normalized
            elif _in_window(timestamp, started_at, ended_at):
                current_usage = normalized
                raw_model = payload.get("model") or info.get("model")
                if isinstance(raw_model, str) and raw_model:
                    model = raw_model
            continue
        if not _in_window(timestamp, started_at, ended_at):
            continue
        if payload_type == "user_message":
            events.append(
                {"type": "user", "timestamp": timestamp, "text": _message_text(payload)}
            )
        elif payload_type == "agent_message":
            events.append(
                {
                    "type": "assistant",
                    "timestamp": timestamp,
                    "text": _message_text(payload),
                }
            )
        elif payload_type in {"reasoning", "thinking"}:
            events.append(
                {
                    "type": "thinking",
                    "timestamp": timestamp,
                    "text": _message_text(payload),
                }
            )
        elif payload_type in {"function_call", "custom_tool_call"}:
            call_id = str(payload.get("call_id") or payload.get("id") or f"tool-{ordinal}")
            event = {
                "type": "tool",
                "timestamp": timestamp,
                "name": str(payload.get("name") or "unknown"),
                "status": "pending",
                "input": payload.get("arguments") or payload.get("input"),
            }
            events.append(event)
            calls[call_id] = event
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            event = calls.get(call_id)
            if event is not None:
                event["ended_at"] = timestamp
                output = payload.get("output")
                failed = payload.get("is_error") is True or (
                    isinstance(output, str)
                    and any(
                        marker in output.lower()
                        for marker in ("exit_code=1", '"exit_code":1', "failed")
                    )
                )
                event["status"] = "failed" if failed else "completed"
                event["error" if failed else "output"] = output
    for event in calls.values():
        if event["status"] == "pending":
            event["status"] = "failed"
            event["error"] = "tool result missing"
    _mark_last_assistant_final(events)
    usage: dict[str, Any] = {}
    if current_usage is not None:
        before = baseline_usage or {key: 0 for key in current_usage}
        if set(before) == set(current_usage):
            delta = {key: current_usage[key] - before[key] for key in current_usage}
            if all(value >= 0 for value in delta.values()):
                usage.update(delta)
    if model:
        usage["model"] = model
    return SupplementWindow(events=events, usage=usage)


def collect_claude_window(
    path: Path,
    *,
    started_at: str,
    ended_at: str,
    max_bytes: int | None = None,
) -> SupplementWindow:
    events: list[dict[str, Any]] = []
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    models: set[str] = set()
    calls: dict[str, dict[str, Any]] = {}
    for ordinal, line in enumerate(_source_lines(path, max_bytes)):
        if ordinal >= MAX_EVENTS:
            raise ValueError("session log event limit exceeded")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        timestamp = raw.get("timestamp")
        if not _in_window(timestamp, started_at, ended_at):
            continue
        event_type = str(raw.get("type") or "")
        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
        if (
            raw.get("isMeta") is True
            or message.get("isMeta") is True
            or message.get("isSidechain") is True
        ):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        text = (
            content
            if isinstance(content, str)
            else "\n".join(
                str(block.get("text"))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        )
        if event_type == "user" and not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in blocks
        ):
            events.append({"type": "user", "timestamp": timestamp, "text": text})
        elif event_type == "assistant":
            events.append({"type": "assistant", "timestamp": timestamp, "text": text})
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            for field in usage_totals:
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    usage_totals[field] += value
            if isinstance(message.get("model"), str):
                models.add(message["model"])
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                call_id = str(block.get("id") or f"tool-{ordinal}")
                event = {
                    "type": "tool",
                    "timestamp": timestamp,
                    "name": str(block.get("name") or "unknown"),
                    "status": "pending",
                    "input": block.get("input"),
                }
                events.append(event)
                calls[call_id] = event
            elif block.get("type") == "tool_result":
                call_id = str(block.get("tool_use_id") or "")
                event = calls.get(call_id)
                if event is not None:
                    event["ended_at"] = timestamp
                    failed = block.get("is_error") is True
                    event["status"] = "failed" if failed else "completed"
                    event["error" if failed else "output"] = block.get("content")
    for event in calls.values():
        if event["status"] == "pending":
            event["status"] = "failed"
            event["error"] = "tool result missing"
    _mark_last_assistant_final(events)
    usage: dict[str, Any] = {key: value for key, value in usage_totals.items() if value}
    if models:
        usage["models"] = sorted(models)
        if len(models) == 1:
            usage["model"] = next(iter(models))
    return SupplementWindow(events=events, usage=usage)
