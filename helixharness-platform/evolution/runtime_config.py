"""Durable, non-secret scheduler settings owned by evolution."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvolutionRuntimeConfiguration:
    """Validated scheduler values that can be changed without editing source."""

    interval_hours: int = 48
    poll_seconds: int = 300
    remediation_poll_seconds: float = 30.0


def runtime_config_path() -> Path:
    """Return the operator-editable runtime config path."""

    configured = os.environ.get("HELIXHARNESS_PLATFORM_EVOLUTION_RUNTIME_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent / ".cache" / "runtime_config.json"


def load_runtime_config() -> EvolutionRuntimeConfiguration:
    """Load saved settings, using environment values only as initial defaults."""

    defaults = EvolutionRuntimeConfiguration(
        interval_hours=_environment_int("HELIXHARNESS_PLATFORM_EVOLUTION_INTERVAL_HOURS", 48),
        poll_seconds=_environment_int("HELIXHARNESS_PLATFORM_EVOLUTION_POLL_SECONDS", 300),
        remediation_poll_seconds=_environment_float(
            "HELIXHARNESS_PLATFORM_EVOLUTION_REMEDIATION_POLL_SECONDS",
            30.0,
        ),
    )
    try:
        raw = json.loads(runtime_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(raw, dict):
        return defaults
    try:
        return validate_runtime_config(raw)
    except ValueError:
        return defaults


def validate_runtime_config(values: dict[str, Any]) -> EvolutionRuntimeConfiguration:
    """Validate form or persisted values with conservative operational bounds."""

    interval_hours = _integer(values.get("interval_hours"), "检查周期", 1, 720)
    poll_seconds = _integer(values.get("poll_seconds"), "后台检查间隔", 1, 86_400)
    remediation_poll_seconds = _number(
        values.get("remediation_poll_seconds"),
        "评测结果回收间隔",
        0.05,
        3_600,
    )
    return EvolutionRuntimeConfiguration(
        interval_hours=interval_hours,
        poll_seconds=poll_seconds,
        remediation_poll_seconds=remediation_poll_seconds,
    )


def save_runtime_config(values: dict[str, Any]) -> EvolutionRuntimeConfiguration:
    """Validate and atomically persist scheduler settings."""

    configuration = validate_runtime_config(values)
    path = runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(configuration), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return configuration


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _number(value: object, label: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label}必须在 {minimum:g} 到 {maximum:g} 之间")
    return parsed


def _environment_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _environment_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default
