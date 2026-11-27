#!/usr/bin/env python3
"""Qoder SessionStart adapter: bind the installed plugin root as Host Context.

Only SessionStart is registered. Qoder telemetry is declared unsupported, so
there is no consent or ledger state that a SessionEnd hook would need to clean,
and stage boundaries stay owned by the public commands.
"""

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
        if not isinstance(payload, dict):
            return 0
        if payload.get("hook_event_name") != "SessionStart":
            return 0
        handle_event(HostRegistry.discover(ROOT / "hosts").get("qoder"), payload)
    except (OSError, ValueError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
