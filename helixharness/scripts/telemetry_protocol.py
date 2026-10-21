#!/usr/bin/env python3
"""Filtering, redaction, and content-addressing for async telemetry supplements."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any


SUPPLEMENT_SCHEMA = "helixharness-stage-supplement/1.0"
EVIDENCE_SCHEMA = "helixharness-evolution-evidence/1.0"

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"), "[REDACTED_SECRET]"),
    (re.compile(r"(?i)\b(?:token|password|passwd|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+"), "[REDACTED_SECRET]"),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
            r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})(?![A-Za-z0-9])"
        ),
        "[REDACTED_SECRET]",
    ),
    (re.compile(r"(?i)\b(?:postgres|mysql|mongodb(?:\+srv)?|redis)://[^\s]+"), "[REDACTED_DSN]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
    (re.compile(r"(?<!\d)(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}(?::\d+)?"), "[REDACTED_HOST]"),
    (re.compile(r"(?i)\b[A-Z]:\\(?:Users|Windows|ProgramData|Temp)\\[^\s,;]+"), "[REDACTED_PATH]"),
    (re.compile(r"(?<![A-Za-z0-9])/(?:Users|home|private|var|tmp|etc|opt|root|srv)/[^\s,;]+"), "[REDACTED_PATH]"),
)
_HIGH_RISK_PATTERNS = (
    re.compile(r"(?is)-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)"),
    re.compile(r"(?is)\*\*\* Begin Patch.*?\*\*\* End Patch"),
    re.compile(r"(?m)^(?:diff --git |@@\s+-\d|Index: |--- a/|\+\+\+ b/).*(?:\n|\Z)"),
    re.compile(r'(?s)\{[^{}]{0,100000}"(?:\\.|[^"\\]){1,512}"\s*:[^{}]{0,100000}\}'),
    re.compile(r"(?m)^-[^\n]*\n\+[^\n]*(?:\n|\Z)"),
    re.compile(r"(?im)\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+.+?\s+SET|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b"),
    re.compile(r"(?is)<([A-Za-z_][\w:.-]*)\b[^>]*>.*?</\1\s*>"),
    re.compile(r"(?m)^\s*[+-]\s*[A-Za-z_$][\w.$\[\]'\"-]*\s*=.+(?:\n|\Z)"),
    re.compile(r"(?m)^\s*[A-Za-z_$][\w.$\[\]'\"-]*\s*=\s*[^=].*(?:\n|\Z)"),
    re.compile(
        r"(?m)^\s*(?:def|class|import|from\s+\S+\s+import|function|const|let|var|"
        r"public|private|protected|package|#include)\b[^\n]*(?:\n|\Z)"
    ),
)
_FENCED_CODE = re.compile(r"(?s)```[^\n]*\n.*?```")


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def sanitize_text(value: Any) -> tuple[str, int]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    redactions = 0
    try:
        structured = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        structured = None
    if isinstance(structured, (dict, list)):
        return "[OMITTED_HIGH_RISK]", 1
    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(text):
            return "[OMITTED_HIGH_RISK]", redactions + 1
    text, code_blocks = _FENCED_CODE.subn("[OMITTED_CODE]", text)
    redactions += code_blocks
    for pattern, replacement in _SECRET_PATTERNS:
        text, count = pattern.subn(replacement, text)
        redactions += count
    for pattern, _ in _SECRET_PATTERNS:
        if pattern.search(text):
            return "[OMITTED_HIGH_RISK]", redactions + 1
    return text, redactions


def _failed_tool_summary(value: Any, *, kind: str) -> tuple[str, int, bool]:
    """Describe failed-tool content without retaining any caller-controlled text."""
    raw = unicodedata.normalize("NFKC", str(value or ""))
    sanitized, redactions = sanitize_text(raw)
    high_risk = sanitized == "[OMITTED_HIGH_RISK]" or "[OMITTED_CODE]" in sanitized
    parsed: Any = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = value
    if kind == "input":
        if isinstance(parsed, dict):
            summary = f"object fields={len(parsed)}"
        elif isinstance(parsed, list):
            summary = f"array items={len(parsed)}"
        elif parsed is None:
            summary = "none"
        else:
            summary = f"{type(parsed).__name__} chars={len(raw)}"
    else:
        lowered = raw.casefold()
        category = next(
            (
                label
                for label, markers in (
                    ("timeout", ("timeout", "timed out", "deadline exceeded")),
                    ("permission", ("permission denied", "forbidden", "unauthorized")),
                    ("not_found", ("not found", "no such file", "404")),
                    ("rate_limit", ("rate limit", "too many requests", "429")),
                    ("network", ("connection", "network", "dns", "socket")),
                    ("exit_code", ("exit code", "returncode", "non-zero")),
                )
                if any(marker in lowered for marker in markers)
            ),
            "unknown",
        )
        summary = f"error category={category} chars={len(raw)}"
    return summary, redactions, high_risk


def build_evolution_evidence(
    events: list[dict[str, Any]], *, started_at: str, ended_at: str
) -> dict[str, Any]:
    start = _time(started_at)
    end = _time(ended_at)
    if start is None or end is None or end < start:
        raise ValueError("invalid evidence window")
    window = [
        event
        for event in events
        if isinstance(event, dict)
        and (timestamp := _time(event.get("timestamp"))) is not None
        and start <= timestamp <= end
    ]
    user_inputs: list[str] = []
    assistant_replies: list[str] = []
    interactions: list[dict[str, str]] = []
    failed_tools: list[dict[str, str]] = []
    tool_succeeded = 0
    tool_failed = 0
    redacted = 0
    omitted = max(0, len(events) - len(window))
    omission_reasons: dict[str, int] = {}

    def record_high_risk_omission(*values: str) -> None:
        nonlocal omitted
        count = sum(value == "[OMITTED_HIGH_RISK]" for value in values)
        if count:
            omitted += count
            omission_reasons["high_risk_content"] = (
                omission_reasons.get("high_risk_content", 0) + count
            )

    for event in window:
        kind = str(event.get("type") or "")
        if kind == "user" and not event.get("meta") and not event.get("tool_result"):
            text, count = sanitize_text(event.get("text"))
            redacted += count
            record_high_risk_omission(text)
            if text:
                user_inputs.append(text)
                interactions.append({"role": "user", "text": text})
        elif kind == "assistant":
            text, count = sanitize_text(event.get("text"))
            redacted += count
            record_high_risk_omission(text)
            if text:
                assistant_replies.append(text)
                interactions.append({"role": "assistant", "text": text})
        elif kind == "tool":
            failed = str(event.get("status") or "").lower() in {"failed", "error", "blocked"}
            if failed:
                tool_failed += 1
                input_summary, input_redactions, input_high_risk = _failed_tool_summary(
                    event.get("input"), kind="input"
                )
                error_summary, error_redactions, error_high_risk = _failed_tool_summary(
                    event.get("error"), kind="error"
                )
                redacted += input_redactions + error_redactions
                high_risk_count = int(input_high_risk) + int(error_high_risk)
                if high_risk_count:
                    omitted += high_risk_count
                    omission_reasons["high_risk_content"] = (
                        omission_reasons.get("high_risk_content", 0)
                        + high_risk_count
                    )
                failed_tools.append(
                    {
                        "name": (
                            str(event.get("name"))[:128]
                            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", str(event.get("name") or ""))
                            else "unknown"
                        ),
                        "input_summary": input_summary,
                        "error_summary": error_summary,
                    }
                )
            else:
                tool_succeeded += 1
                omitted += int(bool(event.get("input"))) + int(bool(event.get("output")))
                if event.get("input") or event.get("output"):
                    omission_reasons["successful_tool_raw_content"] = omission_reasons.get("successful_tool_raw_content", 0) + 1
        else:
            omitted += 1
            reason = "thinking" if kind == "thinking" else "unsupported_event"
            omission_reasons[reason] = omission_reasons.get(reason, 0) + 1

    return {
        "schema": EVIDENCE_SCHEMA,
        "user_inputs": user_inputs,
        "assistant_replies": assistant_replies,
        "interactions": interactions,
        "interaction_summary": {
            "user_turns": len(user_inputs),
            "assistant_messages": len(assistant_replies),
        },
        "tools": {"succeeded": tool_succeeded, "failed": tool_failed},
        "failed_tools": failed_tools,
        "coverage": {
            "candidates": len(events),
            "in_window": len(window),
            "retained": len(user_inputs) + len(assistant_replies) + len(failed_tools),
            "redacted": redacted,
            "omitted": omitted,
            "omission_reasons": omission_reasons,
        },
    }


def build_stage_supplement(
    fixed: dict[str, Any],
    *,
    session_hash: str,
    usage: dict[str, Any],
    execution: dict[str, Any],
    evidence: dict[str, Any],
    workflow_trace: dict[str, Any] | None = None,
    revision: int = 1,
    supersedes: str | None = None,
) -> dict[str, Any]:
    base = {
        "schema": SUPPLEMENT_SCHEMA,
        "revision": int(revision),
        "supersedes": supersedes,
        "entry_id": str(fixed["entry_id"]),
        "requirement_id": str(fixed["requirement_id"]),
        "requirement_name": str(fixed["requirement_name"]),
        "feature": str(fixed.get("feature") or fixed["requirement_name"]),
        "session_hash": str(session_hash),
        "platform": str(fixed["platform"]),
        "session": {
            "platform": str(fixed["platform"]),
            "session_id": fixed.get("session_id"),
            "session_hash": str(session_hash),
            "session_kind": "primary",
        },
        "usage": dict(usage),
        "execution": dict(execution),
        "evidence": dict(evidence),
        "workflow_trace": dict(
            workflow_trace
            or {
                "schema": "helixharness-workflow-trace/1.0",
                "status": "unavailable",
                "reason_code": "trace_unavailable",
            }
        ),
    }
    canonical = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**base, "supplement_id": "supp_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]}
