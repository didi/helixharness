"""Durable, non-secret defaults shared by the evaluation Web UI and workers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def platform_config_path() -> Path:
    """Return the operator-editable config path, keeping it in runtime data."""

    configured = os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent / ".cache" / "platform_config.json"


def load_platform_config() -> dict[str, Any]:
    """Load non-secret defaults; malformed runtime state falls back to empty config."""

    path = platform_config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_platform_config(config: dict[str, Any]) -> Path:
    """Atomically persist validated non-secret defaults."""

    path = platform_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return path
