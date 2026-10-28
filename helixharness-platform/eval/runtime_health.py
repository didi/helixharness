"""Client health v2 metadata contract."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

RUNTIME_HEALTH_V2_SCHEMA = "helixharness-telemetry-runtime-health/2.0"


def timestamp(value: object) -> datetime:
    """Require a timezone; client timestamps must not depend on server locale."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate metadata only; no transcripts, credentials or arbitrary error bodies."""
    required = {
        "schema", "event_id", "runtime_id", "event", "observed_at", "host_os",
        "runtime_version", "scope_kind", "scope_id", "circuit_open", "consecutive_failures",
    }
    optional = {
        "user_name", "repo_name", "platform", "feature", "entry_id", "reason_code",
        "circuit_opened_at", "circuit_resume_at",
    }
    if set(payload) - required - optional or required - set(payload):
        raise ValueError("health v2 contains missing or unsupported fields")
    for key in (required | optional) - {"circuit_open", "consecutive_failures"}:
        if key in payload and (not isinstance(payload[key], str) or not payload[key].strip()
                               or len(payload[key]) > 512):
            raise ValueError(f"{key} must be a non-empty string of at most 512 characters")
    if not payload["event_id"].startswith("health_") or not payload["runtime_id"].startswith("runtime_"):
        raise ValueError("invalid health identity")
    if payload["scope_kind"] not in {"runtime", "collection", "delivery"}:
        raise ValueError("invalid scope_kind")
    if payload["scope_kind"] == "runtime" and payload["scope_id"] != "runtime":
        raise ValueError("runtime scope_id must be runtime")
    if payload["scope_kind"] == "collection" and not {"repo_name", "platform"} <= payload.keys():
        raise ValueError("collection scope requires repo_name and platform")
    if "platform" in payload and payload["platform"] not in {"codex", "claude-code"}:
        raise ValueError("unsupported platform")
    if payload["event"] not in {"circuit_open", "circuit_closed", "heartbeat"}:
        raise ValueError("invalid health event")
    opened = payload["circuit_open"]
    failures = payload["consecutive_failures"]
    if not isinstance(opened, bool) or isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise ValueError("invalid circuit state or failure count")
    if payload["event"] == "circuit_open" and not opened:
        raise ValueError("circuit_open event requires an open circuit")
    if payload["event"] == "circuit_closed" and opened:
        raise ValueError("circuit_closed event requires a closed circuit")
    observed = timestamp(payload["observed_at"])
    if opened:
        if failures < 1 or not {"reason_code", "circuit_opened_at", "circuit_resume_at"} <= payload.keys():
            raise ValueError("open circuit requires failures, reason and circuit times")
    for key in ("circuit_opened_at", "circuit_resume_at"):
        if key in payload:
            timestamp(payload[key])
    if "circuit_opened_at" in payload:
        start = timestamp(payload["circuit_opened_at"])
        if start > observed:
            raise ValueError("circuit opened after observation")
        if "circuit_resume_at" in payload and timestamp(payload["circuit_resume_at"]) < start:
            raise ValueError("circuit resume precedes opening")
    return dict(payload)
