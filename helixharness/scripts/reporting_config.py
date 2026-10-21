"""Published reporting defaults and user-file overrides for both channels."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


# The release owner sets these before packaging. Never infer a recipient.
DEFAULT_REPORT_ENDPOINT = ""
DEFAULT_FEEDBACK_ENDPOINT = ""


def valid_endpoint(endpoint: str) -> bool:
    """Share recipient validation between runtime reads and configuration writes."""
    try:
        parsed = urlsplit(endpoint)
        return bool(
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.port != 0 and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and not any(char.isspace() for char in endpoint)
        )
    except ValueError:
        return False


def endpoint_status(home: Path, default_endpoint: str) -> dict[str, Any]:
    """Resolve user config over the published default, without network or writes."""
    try:
        config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError):
        return {"ready": False, "reason": "endpoint_invalid"}
    if not isinstance(config, dict) or not isinstance(config.get("endpoint", ""), str):
        return {"ready": False, "reason": "endpoint_invalid"}
    endpoint = config.get("endpoint", "").strip() or default_endpoint.strip()
    if not endpoint:
        return {"ready": False, "reason": "endpoint_unconfigured"}
    if not valid_endpoint(endpoint):
        return {"ready": False, "reason": "endpoint_invalid"}
    return {"ready": True, "endpoint": endpoint}
