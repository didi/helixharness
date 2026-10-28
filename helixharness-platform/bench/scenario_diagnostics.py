"""Bounded, redacted diagnostic bundles for terminal Scenario v2 runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_RUN_DIRECTORY = re.compile(r"sv2_[0-9a-f]{32}")
_JOB_ID = re.compile(r"sj_[0-9a-f]{32}")
_SWEEP_STATE_LOCK = threading.Lock()
_LAST_SWEEP_BY_ROOT: dict[Path, float] = {}
_ERROR_KEYS = ("type", "code", "message", "detail")
_EVENT_PAYLOAD_KEYS = frozenset(
    {
        "actions",
        "classification",
        "cycle",
        "decision",
        "error_type",
        "milestone_id",
        "reason",
        "transition",
        "workflow_step_id",
    }
)


@dataclass(frozen=True)
class ScenarioDiagnosticRetentionPolicy:
    """Time windows for small diagnostic bundles after heavy runtime cleanup."""

    terminal_retention: timedelta = timedelta(days=7)
    orphan_retention: timedelta = timedelta(days=30)
    sweep_interval: timedelta = timedelta(minutes=5)

    @classmethod
    def from_environment(cls) -> ScenarioDiagnosticRetentionPolicy:
        """Load non-negative retention seconds while keeping safe defaults."""

        defaults = cls()
        return cls(
            terminal_retention=_configured_duration(
                "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DIAGNOSTIC_RETENTION_SECONDS",
                defaults.terminal_retention,
            ),
            orphan_retention=_configured_duration(
                "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DIAGNOSTIC_ORPHAN_RETENTION_SECONDS",
                defaults.orphan_retention,
            ),
            sweep_interval=_configured_duration(
                "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DIAGNOSTIC_SWEEP_INTERVAL_SECONDS",
                defaults.sweep_interval,
            ),
        )


@dataclass(frozen=True)
class ScenarioDiagnosticCleanupReport:
    """Observable result of one best-effort diagnostic sweep."""

    removed_run_ids: tuple[str, ...] = ()
    retained_run_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class ScenarioDiagnosticRetentionService:
    """Capture safe failure metadata and expire only platform-owned bundles."""

    _TERMINAL_MARKER = ".terminal.json"

    def __init__(
        self,
        diagnostic_root: Path,
        *,
        policy: ScenarioDiagnosticRetentionPolicy | None = None,
    ) -> None:
        self.diagnostic_root = diagnostic_root.resolve()
        self.policy = policy or ScenarioDiagnosticRetentionPolicy.from_environment()

    def capture(
        self,
        *,
        run_id: str,
        job_id: str,
        case_id: str,
        side: str,
        attempt_number: int,
        outcome: dict[str, object],
        captured_at: datetime | None = None,
    ) -> Path:
        """Write one atomic bundle without raw prompts, responses, or code content."""

        _validate_identifier(run_id, _RUN_DIRECTORY, "Scenario Run")
        _validate_identifier(job_id, _JOB_ID, "Scenario Job")
        if side not in {"BASE", "CANDIDATE"}:
            raise ValueError("Scenario diagnostic side is invalid")
        if attempt_number < 1:
            raise ValueError("Scenario diagnostic attempt number is invalid")
        observed_at = captured_at or datetime.now(timezone.utc)
        _require_timezone(observed_at)
        run_root = self._safe_run_root(run_id, create=True)
        target = run_root / f"{job_id}-attempt-{attempt_number}.json"
        temporary = run_root / f".{job_id}-attempt-{attempt_number}.json.tmp"
        if target.is_symlink() or temporary.is_symlink():
            raise OSError("Scenario diagnostic target is unsafe")
        payload = {
            "schema_version": "framework-eval-scenario-diagnostic.v1",
            "captured_at": observed_at.isoformat(),
            "run_id": run_id,
            "job_id": job_id,
            "case_id": case_id[:256],
            "side": side,
            "attempt_number": attempt_number,
            "outcome": _project_outcome(outcome),
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        return target

    def mark_terminal(self, run_id: str, *, completed_at: datetime | None = None) -> None:
        """Start the retention clock only after the durable Run report exists."""

        _validate_identifier(run_id, _RUN_DIRECTORY, "Scenario Run")
        observed_at = completed_at or datetime.now(timezone.utc)
        _require_timezone(observed_at)
        run_root = self._safe_run_root(run_id, create=False)
        if not run_root.is_dir():
            return
        marker = run_root / self._TERMINAL_MARKER
        if marker.is_symlink():
            raise OSError("Scenario diagnostic terminal marker is unsafe")
        if marker.exists():
            return
        temporary = run_root / f".{self._TERMINAL_MARKER}.tmp"
        if temporary.is_symlink():
            raise OSError("Scenario diagnostic terminal marker is unsafe")
        temporary.write_text(
            json.dumps(
                {"completed_at": observed_at.isoformat()},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(marker)

    def sweep_if_due(
        self,
        *,
        now: datetime | None = None,
        monotonic_now: float | None = None,
    ) -> ScenarioDiagnosticCleanupReport | None:
        """Run at most once per configured interval in the current process."""

        current_monotonic = time.monotonic() if monotonic_now is None else monotonic_now
        interval_seconds = self.policy.sweep_interval.total_seconds()
        with _SWEEP_STATE_LOCK:
            last_sweep = _LAST_SWEEP_BY_ROOT.get(self.diagnostic_root)
            if last_sweep is not None and current_monotonic - last_sweep < interval_seconds:
                return None
            _LAST_SWEEP_BY_ROOT[self.diagnostic_root] = current_monotonic
        return self.sweep(now=now)

    def sweep(self, *, now: datetime | None = None) -> ScenarioDiagnosticCleanupReport:
        """Remove expired terminal bundles and abandoned unmarked bundles."""

        if not self.diagnostic_root.is_dir():
            return ScenarioDiagnosticCleanupReport()
        current_time = now or datetime.now(timezone.utc)
        _require_timezone(current_time)
        removed: list[str] = []
        retained: list[str] = []
        errors: list[str] = []
        for path in sorted(self.diagnostic_root.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_dir() or _RUN_DIRECTORY.fullmatch(path.name) is None:
                continue
            try:
                expired = self._expired(path, now=current_time)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            if not expired:
                retained.append(path.name)
                continue
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            removed.append(path.name)
        return ScenarioDiagnosticCleanupReport(
            removed_run_ids=tuple(removed),
            retained_run_ids=tuple(retained),
            errors=tuple(errors),
        )

    def _safe_run_root(self, run_id: str, *, create: bool) -> Path:
        root = self.diagnostic_root
        if root.is_symlink() or (root.exists() and root.resolve() != root):
            raise OSError("Scenario diagnostic root is unsafe")
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(root, 0o700)
        run_root = root / run_id
        if run_root.is_symlink() or run_root.parent != root:
            raise OSError("Scenario diagnostic Run target is unsafe")
        if create:
            run_root.mkdir(exist_ok=True, mode=0o700)
            os.chmod(run_root, 0o700)
        return run_root

    def _expired(self, run_root: Path, *, now: datetime) -> bool:
        marker = run_root / self._TERMINAL_MARKER
        if marker.is_symlink():
            raise OSError("Scenario diagnostic terminal marker is unsafe")
        if not marker.is_file():
            modified_at = datetime.fromtimestamp(run_root.stat().st_mtime, timezone.utc)
            return modified_at <= now - self.policy.orphan_retention
        raw = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("completed_at"), str):
            raise ValueError("Scenario diagnostic terminal marker is invalid")
        completed_at = datetime.fromisoformat(str(raw["completed_at"]))
        _require_timezone(completed_at)
        return completed_at <= now - self.policy.terminal_retention


def _project_outcome(outcome: dict[str, object]) -> dict[str, object]:
    execution = outcome.get("execution") if isinstance(outcome.get("execution"), dict) else {}
    verification = outcome.get("verification") if isinstance(outcome.get("verification"), dict) else {}
    initialization = outcome.get("initialization") if isinstance(outcome.get("initialization"), dict) else {}
    resources = outcome.get("resources") if isinstance(outcome.get("resources"), dict) else {}
    return {
        "terminal_status": _optional_scalar(outcome.get("terminal_status")),
        "case_hard_gates": _string_list(outcome.get("case_hard_gates")),
        "global_hard_gates": _string_list(outcome.get("global_hard_gates")),
        "incompatibility_reasons": _string_list(outcome.get("incompatibility_reasons")),
        "reached_comparable_business_terminal": bool(outcome.get("reached_comparable_business_terminal")),
        "assessment_compatible": bool(outcome.get("assessment_compatible")),
        "resources": {
            key: value
            for key in ("agent_tokens", "active_execution_seconds", "workflow_rework_actions")
            if isinstance((value := resources.get(key)), (int, float)) and not isinstance(value, bool)
        },
        "verification": {
            "status": _optional_scalar(verification.get("status")),
            "checks": [
                _project_check(item)
                for item in _object_list(verification.get("checks"))[:50]
            ],
        },
        "initialization": {"status": _optional_scalar(initialization.get("status"))},
        "evidence": [
            _project_evidence(item)
            for item in _object_list(outcome.get("evidence"))[:100]
        ],
        "execution": {
            "thread_id": _bounded_string(execution.get("thread_id"), 256),
            "turn_count": _optional_integer(execution.get("turn_count")),
            "completed_stage_ids": _string_list(execution.get("completed_stage_ids")),
            "terminal_reason": _bounded_string(execution.get("terminal_reason"), 256),
            "turns": [
                _project_turn(item)
                for item in _object_list(execution.get("turn_evidence"))[:100]
            ],
        },
    }


def _project_check(item: dict[str, Any]) -> dict[str, object]:
    return {
        "check_id": _bounded_string(item.get("check_id"), 256),
        "kind": _bounded_string(item.get("kind"), 128),
        "status": _bounded_string(item.get("status"), 64),
        "duration_ms": _optional_integer(item.get("duration_ms")),
        "timed_out": bool(item.get("timed_out")),
    }


def _project_evidence(item: dict[str, Any]) -> dict[str, object]:
    changed_paths = item.get("changed_paths")
    selected_paths = item.get("selected_paths")
    return {
        "evidence_id": _bounded_string(item.get("evidence_id"), 256),
        "kind": _bounded_string(item.get("kind"), 128),
        "status": _bounded_string(item.get("status"), 64),
        "required": item.get("required") if isinstance(item.get("required"), bool) else None,
        "file_count": _optional_integer(item.get("file_count")),
        "size_bytes": _optional_integer(item.get("size_bytes")),
        "changed_path_count": len(changed_paths) if isinstance(changed_paths, list) else None,
        "selected_path_count": len(selected_paths) if isinstance(selected_paths, list) else None,
    }


def _project_turn(item: dict[str, Any]) -> dict[str, object]:
    final_response = item.get("final_response")
    response_text = final_response if isinstance(final_response, str) else None
    return {
        "turn_sequence": _optional_integer(item.get("turn_sequence")),
        "turn_id": _bounded_string(item.get("turn_id"), 256),
        "input_kind": _bounded_string(item.get("input_kind"), 128),
        "workflow_step_id": _bounded_string(item.get("workflow_step_id"), 256),
        "milestone_id": _bounded_string(item.get("milestone_id"), 256),
        "classification": _bounded_string(item.get("classification"), 64),
        "started_at_ms": _optional_integer(item.get("started_at_ms")),
        "completed_at_ms": _optional_integer(item.get("completed_at_ms")),
        "duration_ms": _optional_integer(item.get("duration_ms")),
        "final_response_length": len(response_text) if response_text is not None else None,
        "final_response_sha256": (
            hashlib.sha256(response_text.encode("utf-8")).hexdigest()
            if response_text is not None
            else None
        ),
        "error": _project_error(item.get("error")),
        "events": [
            _project_event(event)
            for event in _object_list(item.get("events"))[:100]
        ],
    }


def _project_error(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: sanitized
        for key in _ERROR_KEYS
        if (sanitized := _sanitize_text(value.get(key), 1_000)) is not None
    }


def _project_event(item: dict[str, Any]) -> dict[str, object]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    return {
        "event_type": _bounded_string(item.get("event_type"), 128),
        "category": _bounded_string(item.get("category"), 64),
        "status": _bounded_string(item.get("status"), 64),
        "occurred_at_ms": _optional_integer(item.get("occurred_at_ms")),
        "payload": {
            str(key)[:128]: _safe_payload_value(value)
            for key, value in payload.items()
            if key in _EVENT_PAYLOAD_KEYS
        },
    }


def _safe_payload_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_text(value, 1_000)
    if isinstance(value, list):
        return [_safe_payload_value(item) for item in value[:20]]
    return "[OMITTED]"


def _sanitize_text(value: object, limit: int) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value)
    text = re.sub(r"https?://[^\s\"'<>]+", "[REDACTED_URL]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
    text = re.sub(
        r"\b(api[-_ ]?key|authorization|password|secret|access[-_ ]?token)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text[:limit]


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:256] for item in value[:100] if isinstance(item, str)]


def _bounded_string(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _optional_scalar(value: object) -> str | int | float | bool | None:
    return value if isinstance(value, (str, int, float, bool)) else None


def _optional_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _validate_identifier(value: str, pattern: re.Pattern[str], label: str) -> None:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{label} id is invalid")


def _require_timezone(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("Scenario diagnostic retention time must be timezone-aware")


def _configured_duration(name: str, default: timedelta) -> timedelta:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        seconds = int(raw)
    except ValueError:
        return default
    return timedelta(seconds=max(0, seconds))
