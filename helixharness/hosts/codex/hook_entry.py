#!/usr/bin/env python3
"""Codex-native event entrypoint, bound to this installed Host Kit."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from host_runtime import HostRegistry  # noqa: E402
from lifecycle_service import handle_event  # noqa: E402


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("hook_event_name") not in {
            "SessionStart",
            "SessionEnd",
        }:
            return 0
        handle_event(HostRegistry.discover(ROOT / "hosts").get("codex"), payload)
    except (OSError, ValueError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
